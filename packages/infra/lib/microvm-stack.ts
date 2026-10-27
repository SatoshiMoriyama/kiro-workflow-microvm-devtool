import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import { CfnMicrovmImage } from "aws-cdk-lib/aws-lambda";

/**
 * Phase1 最小構成スタック。
 *
 * CDK(IaC) で宣言的に作るのはここまで:
 *  - artifact バケット(code artifact の zip 置き場)
 *  - result バケット(MicroVM が echo 結果を吐く先)
 *  - buildRole(イメージビルド時に assume。artifact read + logs)
 *  - executionRole(実行時に MicroVM が assume。result への PutObject)
 *  - CfnMicrovmImage(al2023 ベース + artifact zip から Firecracker スナップショットをビルド)
 *
 * MicroVM の起動(run-microvm)は実行時 API で、IaC では作れない。scripts/run.sh で起動する。
 */
export class MicrovmStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // --- S3: code artifact 置き場 ---
    const artifactBucket = new s3.Bucket(this, "ArtifactBucket", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // --- S3: 結果出力先 ---
    const resultBucket = new s3.Bucket(this, "ResultBucket", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // code artifact(Dockerfile + アプリ)の zip は scripts/run.sh が
    // この固定キーへアップロードする(CDK の外で管理し URI を透明にする)。
    const artifactKey = "code/app.zip";

    // --- CloudWatch Logs(ビルド/実行ログ) ---
    const logGroup = new logs.LogGroup(this, "MicrovmLogGroup", {
      logGroupName: `/aws/lambda-microvms/kiro-phase1`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // --- IAM: buildRole(イメージビルド時) ---
    const buildRole = new iam.Role(this, "BuildRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description: "MicroVM image build role: read artifact, write logs",
    });
    artifactBucket.grantRead(buildRole);
    logGroup.grantWrite(buildRole);

    // --- IAM: executionRole(MicroVM 実行時) ---
    const executionRole = new iam.Role(this, "ExecutionRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description: "MicroVM execution role: PutObject to result bucket",
    });
    resultBucket.grantPut(executionRole);
    logGroup.grantWrite(executionRole);

    // --- MicroVM イメージ ---
    const region = cdk.Stack.of(this).region;
    const baseImageArn = `arn:aws:lambda:${region}:aws:microvm-image:al2023-1`;
    // al2023-1 の最新 AVAILABLE 版(2026-10 時点で "2")。
    const baseImageVersion = this.node.tryGetContext("baseImageVersion") ?? "2";

    // AWS マネージドのネットワークコネクタ ARN。
    // 形式: arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:<NAME>
    // (出典: MicroVMs Troubleshooting ドキュメントの SHELL_INGRESS の例)
    const connectorArn = (name: string) =>
      `arn:aws:lambda:${region}:aws:network-connector:aws-network-connector:${name}`;
    const internetEgressArn = connectorArn("INTERNET_EGRESS");

    const image = new CfnMicrovmImage(this, "KiroPhase1Image", {
      name: "kiro-phase1",
      description: "Phase1: 起動して echo 結果を S3 に吐く最小 MicroVM イメージ",
      baseImageArn,
      baseImageVersion,
      additionalOsCapabilities: [],
      buildRoleArn: buildRole.roleArn,
      codeArtifact: {
        uri: artifactBucket.s3UrlForObject(artifactKey),
      },
      resources: [{ minimumMemoryInMiB: 512 }],
      cpuConfigurations: [{ architecture: "ARM_64" }],
      egressNetworkConnectors: [internetEgressArn],
      environmentVariables: [
        { key: "RESULT_BUCKET", value: resultBucket.bucketName },
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
          logGroup: logGroup.logGroupName,
        },
      },
    });
    image.node.addDependency(buildRole);

    // --- 出力(scripts/run.sh が参照する値) ---
    new cdk.CfnOutput(this, "ArtifactBucketName", {
      value: artifactBucket.bucketName,
    });
    new cdk.CfnOutput(this, "ArtifactKey", { value: artifactKey });
    new cdk.CfnOutput(this, "ResultBucketName", {
      value: resultBucket.bucketName,
    });
    new cdk.CfnOutput(this, "ExecutionRoleArn", {
      value: executionRole.roleArn,
    });
    new cdk.CfnOutput(this, "MicrovmImageName", { value: image.name });
    new cdk.CfnOutput(this, "LogGroupName", { value: logGroup.logGroupName });
  }
}
