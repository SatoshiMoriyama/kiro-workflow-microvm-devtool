---
inclusion: always
---

<!--
このファイルは MicroVM(AWS Lambda MicroVMs)内で headless 実行される kiro-cli 専用の
グローバル steering。Dockerfile で /root/.kiro/steering/ へ COPY され、KIRO_HOME=/root/.kiro
固定で全 run に常時適用される。
開発者の手元の Kiro IDE が読むワークスペース steering(リポジトリ直下 .kiro/steering/)とは
無関係で、手元 Kiro の挙動には一切影響しない。本文を更新したら再デプロイが必要
(package-image.sh が内容ハッシュを更新 -> CFN 再ビルド)。
-->

# MicroVM 内 kiro-cli 共通注意事項

全 run に共通する最低限の注意事項。

- 作業は指示の範囲に限定し、最小差分で行う。
- 指示に無い既存ファイルを変更・削除しない。
- 生成物(ファイル・ログ・コミットメッセージ)に秘密情報(API キー・トークン・認証情報)を書かない。
- コミットや push を行う場合は、指示されたブランチ・手順にだけ従う。
