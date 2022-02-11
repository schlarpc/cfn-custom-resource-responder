# cfn-custom-resource-responder

This project is a helper for implementing AWS CloudFormation custom resources,
making them easier to write and more robust to failure - without adding any
additional code to your custom resource handlers.

Traditionally, users implement custom resources using an AWS Lambda function to
handle lifecycle events (create, update, delete) sent by CloudFormation.
That Lambda function is responsible for sending its results back to CloudFormation
asynchronously through a provided "response URL". This response process can be fragile,
as incomplete responses or unhandled exceptions in your handler code can temporarily
block your CloudFormation deployment on the misbehaving custom resource.

Other projects have sought to solve this robustness problem, such as the
[Custom Resource Helper](https://github.com/aws-cloudformation/custom-resource-helper) or the
[CDK's Custom Resource Provider Framework](https://docs.aws.amazon.com/cdk/api/v1/docs/custom-resources-readme.html).
However, these projects tackle the problem by either adding code to your handler or by deploying
an additional Lambda function to act as a watchdog for your handler.
`cfn-custom-resource-responder` takes a different approach: the response is routed through
Lambda's "destinations" feature and implemented entirely as templating directives in API Gateway.
This means that it works independently of your handler's runtime and doesn't create any additional
compute resources.

## Usage

Run `cfn_custom_resource_responder.py` to produce a CloudFormation template (or grab it from the releases):

```
$ python3 ./cfn_custom_resource_responder.py > template.json
```

This template can be deployed either as a standalone CloudFormation stack or as a nested stack.
In the former case, the Lambda destination ARN will be exported as `${AWS::StackName}::DestinationARN`.
If used as a nested stack, the ARN can be retrieved by the `Outputs.DestinationARN` attribute.

Once deployed, you can configure your custom resource handler Lambda functions with the responder
destination like so:

```
{
  "Resources": {
    "CustomResourceHandlerRole": {
      "Type": "AWS::IAM::Role",
      "Properties": {
        "Policies": [
          {
            "PolicyName": "destination-put-events",
            "PolicyDocument": {
                "Effect": "Allow",
                "Action": ["events:PutEvents"],
                "Resource": [{"Fn::ImportValue": "CustomResourceResponder::DestinationARN"}]
            }
          }
        ],
        ...
      }
    },
    "CustomResourceHandler": {
      "Type": "AWS::Lambda::Function",
      "Properties": {
        "Role": {"Fn::GetAtt": ["CustomResourceHandlerRole", "Arn"]},
        ...
      }
    },
    "CustomResourceHandlerEventInvokeConfig": {
      "Type": "AWS::Lambda::EventInvokeConfig",
      "Properties": {
        "FunctionName": {"Ref": "CustomResourceHandler"},
        "Qualifier": "$LATEST",
        "DestinationConfig": {
          "OnFailure": {"Fn::ImportValue": "CustomResourceResponder::DestinationARN"},
          "OnSuccess": {"Fn::ImportValue": "CustomResourceResponder::DestinationARN"},
        },
        "MaximumEventAgeInSeconds": 3600,
        ...
      },
    },
    "MyCustomResource": {
      "Type": "Custom::MyCustomResource",
      "Properties": {
        "ServiceToken": {"Fn::GetAtt": ["CustomResourceHandler", "Arn"]},
        ...
      },
      "DependsOn": ["CustomResourceHandlerEventInvokeConfig"]
    }
  }
}
```

Once configured as above, your handler function no longer responds to CloudFormation
lifecycle events by sending JSON to S3. Instead, you simply return a JSON-compatible object
from your Lambda function as you would for a synchronous invocation. The fields of the object
should be the [same as before](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/crpg-ref-responses.html),
but some fields may be omitted for convenience:

* `Status` may be omitted.  Its value defaults to `SUCCESS` if your handler returns normally,
  and defaults to `FAILED` if your handler encounters an unhandled error.
* `Reason` may be omitted. Its value defaults to an empty string if your handler returns normally,
  and defaults to a string containing the `errorType` and `errorMessage` if your handler encounters
  an unhandled error.
* `PhysicalResourceId` may be omitted. For `Create` operations, its value defaults to the
  `RequestId` provided by CloudFormation. For `Update` and `Delete` operations, its value defaults
  to the previous value of `PhysicalResourceId`.
* `StackId`, `RequestId`, and `LogicalResourceId` are ignored and default to the values
  provided by CloudFormation.


## Missing features

* No support for polling or otherwise deferring long-running tasks. Handlers must complete
  their work within one Lambda function invocation.
* Delete-after-failed-create does not have improved ergonomics like in the CDK's framework.
* Some validation is missing compared to the CDK's framework; for instance,
  physical resource IDs are not validated to ensure they remain unchanged on resource deletion.

