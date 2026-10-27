import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import { CfnMicrovmImage } from "aws-cdk-lib/aws-lambda";

export interface ImageStackProps extends cdk.StackProps {
  readonly artifactBucket: s3.IBucket;
  readonly resultBucket: s3.IBucket;
  readonly buildRole: iam.IRole;
  readonly logGroup: logs.ILogGroup;
  readonly artifactKey: string;
}

/**
 * MicroVM イメージスタック。
 *
 * BaseStack をデプロイ -> package-image.sh で artifact zip を投入した後にデプロイする。
 * zip 不在だとビルドが "The artifact was not found in S3." で失敗するため、順序が重要。
 */
export class ImageStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ImageStackProps) {
    super(scope, id, props);

    const region = cdk.Stack.of(this).region;
    const baseImageArn = `arn:aws:lambda:${region}:aws:microvm-image:al2023-1`;
    const baseImageVersion = this.node.tryGetContext("baseImageVersion") ?? "2";

    // AWS マネージドのネットワークコネクタ ARN。
    // 形式: arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:<NAME>
    const connectorArn = (name: string) =>
      `arn:aws:lambda:${region}:aws:network-connector:aws-network-connector:${name}`;
    const internetEgressArn = connectorArn("INTERNET_EGRESS");

    const image = new CfnMicrovmImage(this, "KiroPhase1Image", {
      name: "kiro-phase1",
      description: "Phase1: 起動して echo 結果を S3 に吐く最小 MicroVM イメージ",
      baseImageArn,
      baseImageVersion,
      additionalOsCapabilities: [],
      buildRoleArn: props.buildRole.roleArn,
      codeArtifact: {
        uri: props.artifactBucket.s3UrlForObject(props.artifactKey),
      },
      resources: [{ minimumMemoryInMiB: 512 }],
      cpuConfigurations: [{ architecture: "ARM_64" }],
      egressNetworkConnectors: [internetEgressArn],
      environmentVariables: [
        { key: "RESULT_BUCKET", value: props.resultBucket.bucketName },
      ],
      // ready/run はフックの有効化フラグ(ENABLED/DISABLED)。
      // 実際のパス(/aws/lambda-microvms/runtime/v1/{ready,run})はサーバ側が実装する。
      hooks: {
        port: 8080,
        microvmImageHooks: {
          ready: "ENABLED",
          readyTimeoutInSeconds: 60,
        },
        microvmHooks: {
          run: "ENABLED",
          runTimeoutInSeconds: 60,
        },
      },
      logging: {
        cloudWatch: {
          logGroup: props.logGroup.logGroupName,
        },
      },
    });

    new cdk.CfnOutput(this, "MicrovmImageName", { value: image.name });
    new cdk.CfnOutput(this, "MicrovmImageRef", { value: image.ref });
  }
}
