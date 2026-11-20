# steering 制約の棚卸しと再発防止チェック(Issue #25)

対象: `packages/image/steering/00-global.md`(MicroVM 内 kiro-cli に常時適用される steering)。

## 棚卸し表

| # | 制約 | 機械強制 | 手段 / 不可の理由 |
| --- | --- | --- | --- |
| 1 | 作業は指示の範囲に限定し、最小差分で行う | 不可 | 「指示の範囲」を機械が判定できない。レビューで確認する |
| 2 | 指示に無い既存ファイルを変更・削除しない | 条件付き可 | タスクごとの変更許可リストを渡せれば差分検査できる。現状、許可リストの受け渡しが無いため見送り(判断点 2) |
| 3 | 生成物に秘密情報を書かない | 可(今回実装) | `scripts/check_secrets.py` が追加行の秘密情報パターンを検査 |
| 4 | 指示されたブランチ・手順にだけ従う | 条件付き可 | push 先ブランチ名の検査は可能(`kiro/<requestId>` 以外への push 拒否)。hook_server 側の変更が要るため見送り(判断点 2) |

## 移したチェック(1件目)

- 選定: #3 秘密情報の混入。理由: 判定基準が明確(トークン・鍵の形式)、誤検出が少ない、既存コードの変更が不要、違反の影響が大きい。
- 実体: `scripts/check_secrets.py [BASE_REF]`(`pnpm run check:secrets`)。`BASE_REF...HEAD` の追加行だけを検査。検出時は終了コード 1。
- テスト: `scripts/test_check_secrets.py`(`pnpm run scripts:test`)。
- steering 側の文言を、機械検査される旨に更新済み。

## linter の現状と導入提案

このリポジトリに linter 設定は無い(ESLint / ruff / flake8 / Biome / Prettier、pre-commit、`.github/workflows` とも無し)。勝手に導入せず提案のみ。

| 対象 | 現状 | 提案(要承認) |
| --- | --- | --- |
| hook_server / runs-cli (Python) | pytest 形式のテストあり、linter 無し | ruff の導入(`ruff check`) |
| infra (TypeScript) | `tsconfig.json` のみ、テスト無し | `tsc --noEmit` を check に追加。ESLint は別途判断 |
| CI | 無し | `pnpm run scripts:test` と `pnpm run check:secrets` を回す workflow |

## 判断点(未決。人間が決める)

1. チェック追加の実行先: (a) 別タスクとして MicroVM に投げる、(b) Issue にする。
   - 暫定案: チェックが小さく(1ファイル+テスト)判定基準が明確なら (a)、設計判断を含むなら (b)。SKILL.md の新工程はこの案で記述。
2. 次に steering から移す候補: #2(変更許可リスト)と #4(push 先検査)。どちらも hook_server か run の入力形式の変更を伴う。
3. linter 導入(上表)の可否。
