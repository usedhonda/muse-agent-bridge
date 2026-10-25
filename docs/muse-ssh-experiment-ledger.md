# Muse SSH 実験台帳

目的: 所有者が管理するMuse環境をSSHで観測・修復し、公開資料だけでは分からない実行寿命・永続化・認証・会話処理の実態を、再現できる記録として蓄積する。

再利用可能な実験設計。**この台帳の追加は実機試験の実施・復旧を意味しない。** 実行主体と対象を明記し、同じ確認を並行実行して状態を変えない。

## 証拠の扱い

- `official`: 出典付きの公式仕様。特定VMで成立する証拠ではない。
- `observed`: 実行者自身が取得した結果。端末・実行主体・UTC時刻・対象版を付ける。
- `reported`: 他者から受け取った報告。独立確認までobservedへ昇格しない。
- `hypothesis`: 検証可能な予測と、棄却条件を持つ仮説。
- `unknown`: 未取得・理由不明・結果不明。成功にも失敗原因にも置き換えない。

古い結論は消さず、撤回対象と訂正根拠を追記する。未確認の再試行や、同じ結果を新しい進捗として数えない。引用元は必要最小限の要約とpermalinkのみ。会話本文、個人メモ、資格情報、環境変数全体を保存しない。

## 1回の実験記録

```text
Experiment ID / run ID:
Question / falsifiable hypothesis:
Evidence class:
Operator / source host alias / target host alias / execution identity:
UTC start / end / clock source / known clock skew:
Repo path alias / branch / exact commit / dirty file names:
Preconditions / existing authorization / initial process and work state:
Exact sanitized command or versioned script:
Expected observation / falsifier:
Actual exit code / bounded sanitized output / evidence reference:
Change made (or read-only) / rollback method / rollback result:
Conclusion / excluded alternatives / remaining unknowns:
Outstanding work / next action / owner / next checkpoint:
```

コマンドは実行前に秘密値を出さないことを確認する。秘密値を含む生出力を取得してから後処理する方式は使わない。拒否された操作は別経路で迂回せず、拒否種別と到達段階を証拠にする。

## 実験順序と合格条件

### E01: 接続元とトンネルの同一性（最優先・読取）

接続する端末、既存SSH aliasの実効接続先、ローカル待受アドレス・port・PID・管理元を一組で特定する。秘密値や生のトンネルURLではなく、端末aliasと設定項目名を記録する。localhostの成功は、その端末だけの結果。接続拒否、鍵交換到達、ホスト識別確認、ログイン成功を別チェックポイントにする。

ホスト識別の不一致は無視せず、既存の信頼できる管理情報と照合する。照合不能は未接続のまま残す。相手の返答を待たずにできる接続元側の調査を先に完了する。

合格: どの端末のどの既存接続口が対象VMへ到達するか、観測で説明できる。単発成功は継続稼働合格ではない。

### E02: 実行場所と保存範囲（読取）

SSHで入れたら、既存の権限内でruntime/container/VMの境界、作業ディレクトリ、稼働版、サービス管理機構、関連PID・親PID・cgroupを確認する。runtime内rootをhost管理権限と同一視しない。

合格: コード、設定、待ち列、送信記録、SSH構成の保存場所と管理主体が区別できる。再起動やVM交換後の残存は、この読取だけでは未検証。

### E03: 起動条件と認証（まず読取）

既存wakeの生存確認→listener起動分岐→poll→claim→返信起動を、実際のスクリプトと照合する。起動フラグの名前だけで意図を推測しない。既存ログまたは対応済みの診断から、認証取得を成功・未登録・拒否・一時エラー・区別不能に分類する。値は取得・記録しない。

合格: listener不在の直接条件を特定し、認証失敗とは分離できる。空文字だけなら失敗理由はunknown。ローカル資格情報へのfallbackで診断を覆い隠さない。

### E04: 最小修復と新着1件

E01〜E03の根拠から、1変数ずつの可逆な変更を作る。変更前のコード・設定版と稼働中の仕事を保持し、差分レビューと戻し方を用意する。新しい接続・鍵・権限・認証の移送は、この実験の既存経路修復に含めない。

合格: syntheticな新着1件の元message IDについて、Slack投稿→受信→永続保存→claim→実行→送信受付→Slack読み戻しの時刻が対応する。古い仕事への受領返信は新着確認に数えない。

### E05: 待ち列と最新文脈

古い未完依頼Aの後に訂正Bを置く。返信前に同じ部屋・同じスレッドのBを取得できること、Aと元claimを失わないこと、別室を混ぜないことを確認する。受領だけの反復と実作業の未完を区別する。

合格: 返信が訂正を反映し、未完仕事が消えず、結果不明の送信を再送しない。DBのdoneやpendingゼロだけを完了証拠にしない。

### E06: 寿命と復旧（別実験として順番に）

check正常終了、checkの期限到達、メイン会話動作中、アプリ終了、通信切断を個別に観測する。既存の正式な実行機構を使い、単なるnohup/setsidを寿命保証としない。600秒は設定値だけで終了原因とせず、実際の終了理由と親子の時系列で判定する。

runtime再起動・VM交換は、保存範囲・未完仕事・退避方法を確認し、影響をレビューしてから別途計画する。調査依頼だけで直ちに実行しない。

合格: listenerの継続または再起動、認証の利用可否、保存済み仕事の保持、二重実行・二重送信防止を別々に示す。経過時間だけでclaimedを解除しない。観測窓の長さと、その窓を超えて保証できない点を残す。

## 再現用の最小観測と判定

公開台帳には方法と一般化した結論だけを記載する。実行UTC、正確な対象版、実host/port/PID、unit名、接続情報、投稿ID、queue件数などの稼働固有情報は保護されたローカル記録へ分離する。私的会話、設定全文、認証値は取得・公開しない。

| 観測 | 安全な確認方法 | 結論の限界 |
| --- | --- | --- |
| 既存forward | source側の有効SSH設定と対象portの待受metadataを照合。待受復帰時だけ既存strict経路で無害なreadを1回行う | 単発login成功と、継続して利用可能なforwardは別。対象の取り違えや切断原因は未判定になり得る |
| 実行境界 | 実行主体、UID mapping、対象PIDの親/cgroup、既存unitの状態を限定取得する | runtime内rootやsystemdの存在からhost管理権限・新規serviceの認証可否を推測しない |
| 接続の不安定性 | 既存forwardのrestart回数・開始/終了時刻と、そのunitの非秘密error分類を照合する | `broken pipe`は切断の症状。原因や責任層は未確定。拒否された別のlogを迂回取得しない |
| queueとjobの差 | queueのevent IDをread-only DBの状態と対応付け、現行policyのeligibilityを純粋判定する | jobのpendingゼロやdone件数だけでは未取得・対象外queueの存在を否定できない。doneは外部task完了の証拠ではない |
| 古いroom形式 | 現在の`agentChannels`と、queued recordのrouting metadata有無を本文抜きで照合する | metadata欠落を自動補完せず、過去の送信・実行を確認してから意図的な復旧を設計する |

独立したoffline再現は [`tests/test_poll_agent_rooms.py`](../tests/test_poll_agent_rooms.py)、[`tests/test_worker_status.py`](../tests/test_worker_status.py)、[`tests/test_worker_context.py`](../tests/test_worker_context.py) を参照。これらは実VMのrestartやSocket不通中のbackfill成功を意味しない。

## 記録の保存先

公開資料とruntime一般契約は [`runtime-lifecycle.md`](runtime-lifecycle.md)、対象固有の観測とsanitized evidenceはgitignoredなローカル領域に保存する。公開PRへの転記前に、接続情報、人物・会話・内部運用情報が残っていないことを確認する。peer報告をobservedへ自動昇格せず、稼働状態や原因は検証時刻・対象sourceに限定する。

完了条件は単に「返事が来た」ではなく、観測・修復できた範囲と、runtimeまたは基盤側の対応が必要な範囲を再現可能な証拠で説明できること。
