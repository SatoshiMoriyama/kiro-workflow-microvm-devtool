#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { MicrovmStack } from "../lib/microvm-stack";

const app = new cdk.App();

new MicrovmStack(app, "KiroMicrovmPhase1", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? "ap-northeast-1",
  },
  description:
    "Phase1 最小構成: MicroVM イメージ(echo -> S3) + S3 x2 + IAM x2。run は CLI(scripts/run.sh)で起動する。",
});
