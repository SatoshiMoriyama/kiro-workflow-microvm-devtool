import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";

/**
 * Phase1 基盤スタック(zip 不要で先にデプロイできるもの)。
 *
 *  - artifact バケット(code artifact の zip 置き場。固定名でデプロイ前から参照可)
 *  - result バケット(MicroVM が echo 結果を吐く先)
 *  - buildRole(イメージビルド時に assume)
 *  - executionRole(実行時に MicroVM が assume)
 *  - CloudWatch LogGroup
 *
 * これを deploy -> scripts/package-image.sh で zip 投入 -> ImageStack を deploy、の順で
 * 「artifact が S3 に無い」ビルド失敗を避ける。
 */
export class BaseStack extends cdk.Stack {
  readonly artifactBucket: s3.Bucket;
  readonly resultBucket: s3.Bucket;
  readonly buildRole: iam.Role;
  readonly executionRole: iam.Role;
  readonly logGroup: logs.LogGroup;
  readonly artifactKey = "code/app.zip";

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const account = cdk.Stack.of(this).account;
    const region = cdk.Stack.of(this).region;

    // artifact バケットは固定名にして、ImageStack デプロイ前に
    // package-image.sh が URI を組み立てられるようにする。
    this.artifactBucket = new s3.Bucket(this, "ArtifactBucket", {
      bucketName: `kiro-phase1-artifact-${account}-${region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    this.resultBucket = new s3.Bucket(this, "ResultBucket", {
      bucketName: `kiro-phase1-result-${account}-${region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    this.logGroup = new logs.LogGroup(this, "MicrovmLogGroup", {
      logGroupName: `/aws/lambda-microvms/kiro-phase1`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.buildRole = new iam.Role(this, "BuildRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description: "MicroVM image build role: read artifact, write logs",
    });
    this.artifactBucket.grantRead(this.buildRole);
    this.logGroup.grantWrite(this.buildRole);

    this.executionRole = new iam.Role(this, "ExecutionRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description: "MicroVM execution role: PutObject to result bucket",
    });
    this.resultBucket.grantPut(this.executionRole);
    this.logGroup.grantWrite(this.executionRole);

    new cdk.CfnOutput(this, "ArtifactBucketName", {
      value: this.artifactBucket.bucketName,
      exportName: "KiroPhase1-ArtifactBucketName",
    });
    new cdk.CfnOutput(this, "ArtifactKey", { value: this.artifactKey });
    new cdk.CfnOutput(this, "ResultBucketName", {
      value: this.resultBucket.bucketName,
      exportName: "KiroPhase1-ResultBucketName",
    });
    new cdk.CfnOutput(this, "ExecutionRoleArn", {
      value: this.executionRole.roleArn,
      exportName: "KiroPhase1-ExecutionRoleArn",
    });
    new cdk.CfnOutput(this, "LogGroupName", {
      value: this.logGroup.logGroupName,
    });
  }
}
