import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";

/**
 * 基盤スタック(zip 不要で先にデプロイできるもの)。
 *
 *  - artifact バケット(code artifact の zip 置き場。固定名でデプロイ前から参照可)
 *  - result バケット(MicroVM が成果物を吐く先)
 *  - buildRole(イメージビルド時に assume)
 *  - executionRole(実行時に MicroVM が assume)
 *  - CloudWatch LogGroup
 *  - KIRO_API_KEY の器(Secrets Manager)
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
  readonly apiKeySecret: secretsmanager.Secret;
  readonly githubTokenSecret: secretsmanager.Secret;
  readonly artifactKey = "code/app.zip";

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const account = cdk.Stack.of(this).account;
    const region = cdk.Stack.of(this).region;

    // artifact バケットは固定名にして、ImageStack デプロイ前に
    // package-image.sh が URI を組み立てられるようにする。
    this.artifactBucket = new s3.Bucket(this, "ArtifactBucket", {
      bucketName: `kiro-microvm-artifact-${account}-${region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    this.resultBucket = new s3.Bucket(this, "ResultBucket", {
      bucketName: `kiro-microvm-result-${account}-${region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      // 入力プロンプト(<requestId>/inputs/prompt.md)は kind=input タグを付けて
      // PUT される想定。入力は一過性なので、タグ付きオブジェクトのみ 7 日で
      // 自動失効させる(成果物 <requestId>/runs/ は対象外)。
      lifecycleRules: [
        {
          id: "expire-input-prompts",
          enabled: true,
          tagFilters: { kind: "input" },
          expiration: cdk.Duration.days(7),
        },
      ],
    });

    this.logGroup = new logs.LogGroup(this, "MicrovmLogGroup", {
      logGroupName: `/aws/lambda-microvms/kiro-microvm`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.buildRole = new iam.Role(this, "BuildRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description: "MicroVM image build role: read artifact, write logs",
    });
    this.artifactBucket.grantRead(this.buildRole);
    this.logGroup.grantWrite(this.buildRole);

    // KIRO_API_KEY を入れる器。初期はランダムダミー値(CDK 自動生成)で、
    // 実キーは後から `aws secretsmanager put-secret-value` で上書きする。
    // 実キーは IaC/テンプレート/git に一切載せない。
    this.apiKeySecret = new secretsmanager.Secret(this, "KiroApiKeySecret", {
      secretName: "kiro-microvm/kiro-api-key",
      description: "Kiro CLI の KIRO_API_KEY(値は put-secret-value で後から投入)",
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // GitHub PAT を入れる器(KIRO_API_KEY と同思想)。実トークンは IaC/git に
    // 焼かず、後から put-secret-value で投入する。方式A: ゲストの GIT_ASKPASS
    // helper がこの Secret を実行時に取得し git に渡す(Kiro には見せない)。
    this.githubTokenSecret = new secretsmanager.Secret(this, "GithubTokenSecret", {
      secretName: "kiro-microvm/github-token",
      description: "GitHub PAT(git push 用。値は put-secret-value で後から投入)",
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.executionRole = new iam.Role(this, "ExecutionRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description: "MicroVM execution role: PutObject to result, read API key secret",
    });
    // result バケットへのアクセスは最小権限で明示的に絞る:
    //  - 入力プロンプトの取得は <bucket>/*/inputs/* のみ (s3:GetObject)
    //  - 成果物の書き込みは <bucket>/*/runs/*  のみ (s3:PutObject)
    // grantPut による広い許可は使わない。
    this.executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ReadInputPromptsOnly",
        effect: iam.Effect.ALLOW,
        actions: ["s3:GetObject"],
        resources: [`${this.resultBucket.bucketArn}/*/inputs/*`],
      }),
    );
    this.executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "WriteRunOutputsOnly",
        effect: iam.Effect.ALLOW,
        actions: ["s3:PutObject"],
        resources: [`${this.resultBucket.bucketArn}/*/runs/*`],
      }),
    );
    this.logGroup.grantWrite(this.executionRole);
    // ゲストが短命 credential で KIRO_API_KEY を取得できるようにする。
    this.apiKeySecret.grantRead(this.executionRole);
    // GitHub PAT も同じ短命 credential で取得できるようにする(GIT_ASKPASS 経由)。
    this.githubTokenSecret.grantRead(this.executionRole);

    new cdk.CfnOutput(this, "ArtifactBucketName", {
      value: this.artifactBucket.bucketName,
      exportName: "KiroMicrovm-ArtifactBucketName",
    });
    new cdk.CfnOutput(this, "ArtifactKey", { value: this.artifactKey });
    new cdk.CfnOutput(this, "ResultBucketName", {
      value: this.resultBucket.bucketName,
      exportName: "KiroMicrovm-ResultBucketName",
    });
    new cdk.CfnOutput(this, "ExecutionRoleArn", {
      value: this.executionRole.roleArn,
      exportName: "KiroMicrovm-ExecutionRoleArn",
    });
    new cdk.CfnOutput(this, "ApiKeySecretArn", {
      value: this.apiKeySecret.secretArn,
      exportName: "KiroMicrovm-ApiKeySecretArn",
    });
    new cdk.CfnOutput(this, "GithubTokenSecretArn", {
      value: this.githubTokenSecret.secretArn,
      exportName: "KiroMicrovm-GithubTokenSecretArn",
    });
    new cdk.CfnOutput(this, "LogGroupName", {
      value: this.logGroup.logGroupName,
    });
  }
}
