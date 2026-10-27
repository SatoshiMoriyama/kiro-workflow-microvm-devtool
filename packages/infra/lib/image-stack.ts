import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import { CfnMicrovmImage } from "aws-cdk-lib/aws-lambda";
import * as fs from "node:fs";
import * as path from "node:path";

export interface ImageStackProps extends cdk.StackProps {
  readonly artifactBucket: s3.IBucket;
  readonly resultBucket: s3.IBucket;
  readonly buildRole: iam.IRole;
  readonly logGroup: logs.ILogGroup;
}

/**
 * MicroVM イメージスタック。
 *
 * BaseStack をデプロイ -> package-image.sh で artifact zip を投入した後にデプロイする。
 * codeArtifact のキーは zip の内容ハッシュ(artifact-hash.txt)を含む。zip が変われば
 * キー(= URI)が変わり CFN が差分を検知して再ビルドする。CLI の update-microvm-image は
 * 使わない(環境変数や hooks を落とす事故を避けるため、更新は常に CFN 経由)。
 */
export class ImageStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ImageStackProps) {
    super(scope, id, props);

    // package-image.sh が書き出したハッシュを読む。未生成ならビルド前なのでエラーにする。
    const hashFile = path.join(__dirname, "..", "artifact-hash.txt");
    if (!fs.existsSync(hashFile)) {
      throw new Error(
        `artifact-hash.txt が無い。先に scripts/package-image.sh を実行する(${hashFile})`,
      );
    }
    const artifactHash = fs.readFileSync(hashFile, "utf-8").trim();
    const artifactKey = `code/app-${artifactHash}.zip`;

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
        uri: props.artifactBucket.s3UrlForObject(artifactKey),
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
    new cdk.CfnOutput(this, "ArtifactKey", { value: artifactKey });
  }
}
