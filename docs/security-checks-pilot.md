# PRで自動実行されるセキュリティチェック

この試験導入の対象は **`usedhonda/muse-agent-bridge`** です。

## 使い方

1. このリポジトリの最新の `main` から作業ブランチを作り、普段どおり `main` 宛てのPRを出す。
2. PRの **Checks → Security checks → CodeQL (Python)** を開く。
3. 指摘を確認して同じブランチを更新すると、自動で再検査される。

READMEを1行変えるだけでも起動します。故意の脆弱性や実際の秘密情報を入れて試す必要はありません。文書だけの変更は起動確認であり、脆弱性の検出能力を試したことにはなりません。
外部Forkから初めて貢献する場合などは、GitHub側で実行承認が必要になる場合があります。

## 今回入れたもの

**CodeQL (Python)** による静的解析です。標準のセキュリティクエリを使用し、`main` 宛てのPR作成・更新・再オープンと `main` へのpushで実行します。既存の **Offline tests** は変更せず残します。

解析完了（緑色）は「全て安全」の保証ではありません。PRのCode scanningの指摘も確認してください。`main` の結果は、このリポジトリの **Security → Code scanning** に保存されます。

## 追加登録なしで試すための範囲

新しいAPIキー、PAT、暗号鍵、ZIP、専用Web画面は使いません。OpenAI APIは呼びません。公開リポジトリの標準GitHub-hosted runnerで動く構成です。

**依存ライブラリ検査は今回含みません。** 導入PRでDependency Reviewも試しましたが、このリポジトリではDependency graphが無効のため失敗しました。追加設定を利用者に要求しない最小試験として、最終構成は実動作を確認したCodeQLに限定します。失敗を無視して緑色にする処理は入れていません。

自動修正、自動マージ、ブランチ保護・必須チェックの変更、他リポジトリへの一括導入、Secret scanning/Push protection設定変更も含みません。`security-review` の手動AI検査・既存Secretは変更しません。

公開PRの標準チェックやコードの注釈は、公開閲覧者にも見える場合があります。非公開レポートの転記ではなくGitHub標準のレビュー導線です。追加したジョブにはPRの任意コマンド実行や `pull_request_target` はありません。ActionはコミットSHAで固定しています。

## 一次資料

- [CodeQL Action](https://github.com/github/codeql-action)
- [Dependency Reviewの前提条件](https://github.com/actions/dependency-review-action)
- [GitHub Actionsの料金](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
