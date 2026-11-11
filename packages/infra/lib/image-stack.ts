import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { CfnMicrovmImage } from "aws-cdk-lib/aws-lambda";
import * as fs from "node:fs";
import * as path from "node:path";

export interface ImageStackProps extends cdk.StackProps {
  readonly artifactBucket: s3.IBucket;
  readonly resultBucket: s3.IBucket;
  readonly buildRole: iam.IRole;
  readonly logGroup: logs.ILogGroup;
  readonly apiKeySecret: secretsmanager.ISecret;
  readonly githubTokenSecret: secretsmanager.ISecret;
  /**
   * MicroVM 内 kiro-cli の実装コミットの author 名。
   * 未指定時はイメージ側既定の kiro-bot。
   */
  readonly gitAuthorName?: string;
  /**
   * MicroVM 内 kiro-cli の実装コミットの author メール。
   * 未指定時はイメージ側既定の kiro-bot@users.noreply.github.com。
   */
  readonly gitAuthorEmail?: string;
}

// 禁止文字: 制御文字(U+0000-U+001F, U+007F)と " \ ` $ < >。
// 値は PROMPT 内の `git config user.name "<値>"` としてシェル実行されるため、
// 引用・展開を壊す文字を拒否する。< > は git の ident で使えない。
// packages/image/hook_server.py の検証規則と同一にすること。
const FORBIDDEN_CHARS = /[\x00-\x1f\x7f"\\`$<>]/;
const EMAIL_FORMAT = /^[^@\s]+@[^@\s]+$/;

/** trim して空なら undefined。不正なら Error を throw する。 */
function validateGitAuthor(
  kind: "name" | "email",
  inputName: string,
  raw: string | undefined,
): string | undefined {
  const value = raw?.trim();
  if (!value) return undefined;
  if (FORBIDDEN_CHARS.test(value)) {
    throw new Error(
      `${inputName} is invalid: must not contain control characters or any of " \\ \` $ < > (value: ${JSON.stringify(value)})`,
    );
  }
  if (kind === "email" && !EMAIL_FORMAT.test(value)) {
    throw new Error(
      `${inputName} is invalid: must match ${EMAIL_FORMAT} (value: ${JSON.stringify(value)})`,
    );
  }
  return value;
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

    // package-image.sh が書き出したハッシュを読む。
    // 未生成でも app のロード(= 他スタックの deploy や synth)は通す必要があるため
    // ここでは throw しない。ハッシュ未生成のときはプレースホルダキーにしておき、
    // ImageStack を実際に deploy する前に package-image.sh でハッシュを生成する
    // 運用にする(deploy.sh が Base -> package-image.sh -> Image の順で保証する)。
    // プレースホルダのまま deploy すると存在しない zip を指してビルドが
    // "The artifact was not found in S3." で失敗するので、気付ける。
    const hashFile = path.join(__dirname, "..", "artifact-hash.txt");
    const artifactHash = fs.existsSync(hashFile)
      ? fs.readFileSync(hashFile, "utf-8").trim()
      : "PLACEHOLDER-run-package-image-sh-first";
    const artifactKey = `code/app-${artifactHash}.zip`;

    const region = cdk.Stack.of(this).region;
    const baseImageArn = `arn:aws:lambda:${region}:aws:microvm-image:al2023-1`;
    const baseImageVersion = this.node.tryGetContext("baseImageVersion") ?? "2";

    // AWS マネージドのネットワークコネクタ ARN。
    // 形式: arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:<NAME>
    const connectorArn = (name: string) =>
      `arn:aws:lambda:${region}:aws:network-connector:aws-network-connector:${name}`;
    const internetEgressArn = connectorArn("INTERNET_EGRESS");

    const gitAuthorName = validateGitAuthor(
      "name",
      "gitAuthorName / KIRO_GIT_AUTHOR_NAME",
      props.gitAuthorName,
    );
    const gitAuthorEmail = validateGitAuthor(
      "email",
      "gitAuthorEmail / KIRO_GIT_AUTHOR_EMAIL",
      props.gitAuthorEmail,
    );

    const image = new CfnMicrovmImage(this, "KiroMicrovmImage", {
      name: "kiro-microvm",
      description:
        "MicroVM 上で Kiro CLI を headless 実行し成果物を S3 に格納する",
      baseImageArn,
      baseImageVersion,
      additionalOsCapabilities: [],
      buildRoleArn: props.buildRole.roleArn,
      codeArtifact: {
        uri: props.artifactBucket.s3UrlForObject(artifactKey),
      },
      resources: [{ minimumMemoryInMiB: 2048 }],
      cpuConfigurations: [{ architecture: "ARM_64" }],
      egressNetworkConnectors: [internetEgressArn],
      environmentVariables: [
        { key: "RESULT_BUCKET", value: props.resultBucket.bucketName },
        // 実キーは焼かず ARN のみ注入する。/run 実行時に executionRole の
        // 短命 credential でゲストが Secrets Manager から値を取得する。
        {
          key: "KIRO_API_KEY_SECRET_ARN",
          value: props.apiKeySecret.secretArn,
        },
        // GitHub PAT も ARN のみ注入。ゲストの GIT_ASKPASS helper が実行時に取得する。
        {
          key: "GITHUB_TOKEN_SECRET_ARN",
          value: props.githubTokenSecret.secretArn,
        },
        // デプロイ時設定のコミット author。未指定ならイメージ側(hook_server.py)既定の kiro-bot。
        ...(gitAuthorName
          ? [{ key: "KIRO_GIT_AUTHOR_NAME", value: gitAuthorName }]
          : []),
        ...(gitAuthorEmail
          ? [{ key: "KIRO_GIT_AUTHOR_EMAIL", value: gitAuthorEmail }]
          : []),
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
          // RunTimeoutInSeconds は上限 60 秒(サービスモデル min=1/max=60)。
          // 非同期モデルでは /run は Kiro をバックグラウンド起動して即 200 を返すので
          // 60 で十分。Kiro の実行時間は MicroVM 寿命(run-microvm の
          // --maximum-duration-in-seconds)が担保する。
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
