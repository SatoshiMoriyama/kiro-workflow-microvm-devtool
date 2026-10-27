#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { BaseStack } from "../lib/base-stack";
import { ImageStack } from "../lib/image-stack";

const app = new cdk.App();

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION ?? "ap-northeast-1",
};

// 基盤(バケット/IAM/ログ)。zip 不要で先にデプロイできる。
const base = new BaseStack(app, "KiroMicrovmPhase1Base", {
  env,
  description:
    "Phase1 基盤: S3 x2 + IAM x2 + LogGroup。zip 投入前にデプロイする。",
});

// MicroVM イメージ。zip 投入後にデプロイする(順序が重要)。
const image = new ImageStack(app, "KiroMicrovmPhase1Image", {
  env,
  description:
    "Phase1 イメージ: CfnMicrovmImage。BaseStack デプロイ + zip 投入後にデプロイ。",
  artifactBucket: base.artifactBucket,
  resultBucket: base.resultBucket,
  buildRole: base.buildRole,
  logGroup: base.logGroup,
  artifactKey: base.artifactKey,
});
image.addStackDependency(base);
