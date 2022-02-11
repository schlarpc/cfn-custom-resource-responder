#!/usr/bin/env python3

import hashlib
import json
import textwrap

from awacs import execute_api, sts
from awacs.aws import Allow, PolicyDocument, Principal, Statement
from troposphere import (
    AccountId,
    Export,
    Join,
    Output,
    Partition,
    Region,
    Split,
    StackName,
    Template,
    URLSuffix,
    encode_to_dict,
)
from troposphere.apigateway import Deployment, EndpointConfiguration, RestApi, Stage
from troposphere.events import EventBus, RetryPolicy, Rule, Target
from troposphere.iam import Policy, Role


def hash_resource(resource, length=12):
    resource_bytes = json.dumps(encode_to_dict(resource), sort_keys=True).encode(
        "utf-8"
    )
    return hashlib.sha256(resource_bytes).hexdigest().upper()[:length]


def create_template():
    template = Template(Description="https://github.com/schlarpc/cfn-custom-resource-responder")

    # TODO things CDK does that we don't:
    # * handle delete-after-failed-create
    # * allow callbacks for longer tasks
    # * handling for "if we are in DELETE and physical ID was changed"

    vtl_template = textwrap.dedent(
        r"""
        #set($details = $input.path('$.detail'))
        #set($fullPath = $details.requestPayload.ResponseURL.split('/', 4)[3])
        #set($context.requestOverride.path.path = $util.urlDecode($fullPath.split('\?', 2)[0]))
        #foreach($qsPair in $fullPath.split('\?', 2)[1].split('&'))
            #set($qsKey = $util.urlDecode($qsPair.split('=', 2)[0]))
            #set($qsValue = $util.urlDecode($qsPair.split('=', 2)[1]))
            #set($context.requestOverride.querystring[$qsKey] = $qsValue)
        #end
        {
            #if ($details.responseContext.containsKey('functionError'))
                #set($errorType = $util.escapeJavaScript($details.responsePayload.errorType).replaceAll("\\'","'"))
                #set($errorMessage = $util.escapeJavaScript($details.responsePayload.errorMessage).replaceAll("\\'","'"))
                "Status": "FAILED",
                "Reason": "Unhandled error: $errorType: $errorMessage",
            #else
                "Status": "SUCCESS",
                "Reason": "",
            #end

            #if ($details.requestPayload.containsKey('PhysicalResourceId'))
                "PhysicalResourceId": $input.json('$.detail.requestPayload.PhysicalResourceId'),
            #else
                "PhysicalResourceId": $input.json('$.detail.requestPayload.RequestId'),
            #end

            #if (!$details.responseContext.containsKey('functionError'))
                #foreach($responseKey in $details.responsePayload.keySet())
                    #set($escapedResponseKey = $util.escapeJavaScript($responseKey).replaceAll("\\'","'"))
                    "$escapedResponseKey": $input.json("$.detail.responsePayload.$responseKey"),
                #end
            #end

            "StackId": $input.json('$.detail.requestPayload.StackId'),
            "RequestId": $input.json('$.detail.requestPayload.RequestId'),
            "LogicalResourceId": $input.json('$.detail.requestPayload.LogicalResourceId')
        }
        """
    ).strip("\n")

    api = RestApi(
        "Api",
        EndpointConfiguration=EndpointConfiguration(Types=["REGIONAL"]),
        FailOnWarnings=True,
        Body={
            "swagger": "2.0",
            "info": {"title": Join("-", [StackName, "CustomResourceResponderAPI"])},
            "paths": {
                "/": {
                    "put": {
                        "consumes": ["application/json"],
                        "produces": ["application/json"],
                        "security": [{"sigv4": []}],
                        "responses": {
                            "200": {"description": "200 response"},
                            "500": {"description": "500 response"},
                        },
                        "x-amazon-apigateway-integration": {
                            "type": "http",
                            "httpMethod": "PUT",
                            "uri": Join(
                                "",
                                [
                                    "https://cloudformation-custom-resource-response-",
                                    Join("", Split("-", Region)),
                                    ".s3.",
                                    URLSuffix,
                                    "/{path}",
                                ],
                            ),
                            "passthroughBehavior": "never",
                            "requestTemplates": {
                                "application/json": vtl_template,
                            },
                            "responses": {
                                "default": {"statusCode": "200"},
                                "[^2]\\d{2}": {"statusCode": "500"},
                            },
                        },
                    }
                }
            },
            "securityDefinitions": {
                "sigv4": {
                    "type": "apiKey",
                    "name": "Authorization",
                    "in": "header",
                    "x-amazon-apigateway-authtype": "awsSigv4",
                }
            },
        },
    )

    api_hash = hash_resource(api)
    api.title += api_hash
    template.add_resource(api)

    deployment = template.add_resource(
        Deployment(
            f"Deployment{api_hash}",
            RestApiId=api.ref(),
        )
    )

    stage = template.add_resource(
        Stage(
            f"Stage{api_hash}",
            RestApiId=api.ref(),
            DeploymentId=deployment.ref(),
            StageName="api",
        )
    )

    endpoint_arn = Join(
        ":",
        [
            "arn",
            Partition,
            "execute-api",
            Region,
            AccountId,
            Join("/", [api.ref(), stage.ref(), "PUT", ""]),
        ],
    )

    event_bus = template.add_resource(
        EventBus(
            "EventBus",
            Name=Join("-", [StackName, "CustomResourceResponderEventBus"]),
        )
    )

    rule_target_role = template.add_resource(
        Role(
            "RuleTargetRole",
            AssumeRolePolicyDocument=PolicyDocument(
                Statement=[
                    Statement(
                        Effect=Allow,
                        Principal=Principal("Service", "events.amazonaws.com"),
                        Action=[sts.AssumeRole],
                    ),
                ],
            ),
            Policies=[
                Policy(
                    PolicyName=f"Invoke{api.title}",
                    PolicyDocument=PolicyDocument(
                        Statement=[
                            Statement(
                                Effect=Allow,
                                Action=[execute_api.Invoke],
                                Resource=[endpoint_arn],
                            ),
                        ],
                    ),
                ),
            ],
        )
    )

    rule = template.add_resource(
        Rule(
            "Rule",
            EventBusName=event_bus.ref(),
            EventPattern={
                "detail-type": [
                    "Lambda Function Invocation Result - Success",
                    "Lambda Function Invocation Result - Failure",
                ],
                "source": [
                    "lambda",
                ],
            },
            Targets=[
                Target(
                    Id=f"Invoke{api.title}",
                    Arn=endpoint_arn,
                    RoleArn=rule_target_role.get_att("Arn"),
                    RetryPolicy=RetryPolicy(MaximumEventAgeInSeconds=3600),
                ),
            ],
        )
    )

    template.add_output(
        Output(
            "DestinationARN",
            Value=event_bus.get_att("Arn"),
            Export=Export(Join("::", [StackName, "DestinationARN"])),
        )
    )

    return template


if __name__ == "__main__":
    print(create_template().to_json())
