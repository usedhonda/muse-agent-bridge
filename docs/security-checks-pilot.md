# PRで自動実行されるセキュリティチェック

この試験導入の対象は **`usedhonda/muse-agent-bridge`** です。

## 使い方

1. このリポジトリの最新の `main` から作業ブランチを作り、普段どおりPRを出す。
2. PRの **Checks** で **Security checks** を開く。
3. 指摘を確認して同じブランチを更新すると、自動で再検査される。

試しにREADMEを1行変えるだけでも起動します。危険なコードや実際の秘密情報を入れて試す必要はありません。
初めて外部Forkから貢献する場合などは、GitHub側で実行承認が必要になる場合があります。

## 今回入れたもの

| チェック名 | 実行タイミング | 内容 |
| --- | --- | --- |
| CodeQL (Python) | `main` 宛てのPR作成・更新・再オープン、`main` へのpush、手動実行 | Pythonコードを標準のセキュリティクエリで静的解析 |
| Dependency review | `main` 宛てのPR | 追加・変更される依存関係の既知の脆弱性を調べ、High/Criticalで失敗にする |

既存の **Offline tests** はそのまま残ります。各ジョブは別のチェックなので、テスト成功とセキュリティ指摘の有無は区別してください。

CodeQLの解析完了（緑色）は「全て安全」の保証ではありません。PRのCode scanningの指摘も確認します。`main` の結果はこのリポジトリの **Security → Code scanning** で確認できます。Dependency reviewも依存関係の変更がないPRでは、新しい問題がないという結果になります。既存依存関係すべての監査や、未登録の依存関係まで保証するものではありません。

## 追加操作・料金・範囲

新しいAPIキー、PAT、暗号鍵、ZIP、専用Web画面は使いません。OpenAI APIは呼びません。公開リポジトリの標準GitHub-hosted runnerで動く構成です。

この導入は **検査の自動実行だけ** です。自動修正、自動マージ、ブランチ保護・必須チェックの設定変更、他リポジトリへの一括導入、Secret scanning/Push protectionの設定変更は含みません。`security-review` の手動AI検査・既存Secretも変更しません。

公開PRの標準チェック、依存関係の指摘、コードの注釈は他の閲覧者にも見える場合があります。秘密の完全レポートを配る仕組みではなく、GitHub標準のレビュー導線を使います。PRの内容を使った任意コマンド実行や、`pull_request_target` は追加していません。利用するActionはコミットSHAで固定しています。

## 一次資料

- [CodeQL Action](https://github.com/github/codeql-action)
- [Dependency Review Action](https://github.com/actions/dependency-review-action)
- [GitHub Actionsの料金](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
