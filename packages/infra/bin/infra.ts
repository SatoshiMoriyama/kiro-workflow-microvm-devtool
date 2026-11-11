#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { BaseStack } from "../lib/base-stack";
import { ImageStack } from "../lib/image-stack";

const app = new cdk.App();

// デプロイ時入力の解決。優先順位: CDK コンテキスト(-c key=value) > 環境変数。
// 値は trim し、空文字は未指定扱い。どちらも無ければ undefined。
function resolveInput(contextKey: string, envKey: string): string | undefined {
  const ctx = app.node.tryGetContext(contextKey);
  if (ctx !== undefined && ctx !== null) {
    const v = String(ctx).trim();
    if (v) return v;
  }
  const e = process.env[envKey]?.trim();
  return e ? e : undefined;
}

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION ?? "ap-northeast-1",
};

// 基盤(バケット/IAM/ログ/Secret)。zip 不要で先にデプロイできる。
const base = new BaseStack(app, "KiroMicrovmBase", {
  env,
  description:
    "基盤: S3 x2 + IAM x2 + LogGroup + Secret。zip 投入前にデプロイする。",
});

// MicroVM イメージ。zip 投入後にデプロイする(順序が重要)。
const image = new ImageStack(app, "KiroMicrovmImage", {
  env,
  description:
    "イメージ: CfnMicrovmImage。BaseStack デプロイ + zip 投入後にデプロイ。",
  artifactBucket: base.artifactBucket,
  resultBucket: base.resultBucket,
  buildRole: base.buildRole,
  logGroup: base.logGroup,
  apiKeySecret: base.apiKeySecret,
  githubTokenSecret: base.githubTokenSecret,
  // コミット author(MicroVM 内 kiro-cli)。
  // 指定方法: `-c gitAuthorName=... -c gitAuthorEmail=...` または
  // 環境変数 KIRO_GIT_AUTHOR_NAME / KIRO_GIT_AUTHOR_EMAIL(コンテキスト優先)。
  // 未指定ならイメージ側既定の kiro-bot / kiro-bot@users.noreply.github.com。
  gitAuthorName: resolveInput("gitAuthorName", "KIRO_GIT_AUTHOR_NAME"),
  gitAuthorEmail: resolveInput("gitAuthorEmail", "KIRO_GIT_AUTHOR_EMAIL"),
});
image.addStackDependency(base);
