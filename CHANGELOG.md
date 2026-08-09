# Changelog

`hapbeat-python-sdk`（import 名: `hapbeat`）の主要な変更点をまとめます。

## 0.3.0 — マルチホーム環境での探索・送信の修正

Unity SDK v0.4.0 で確定した接続まわりの修正を移植した。

### 修正
- **マルチホーム PC でデバイスを 1 台も発見できない問題**を修正した。
  `255.255.255.255`（limited broadcast）は**インターフェイスメトリックが最小の
  1 本からしか送出されない**。Hyper-V / WSL2 / Docker を入れると作られる仮想
  アダプタは **LAN ケーブルを繋いでいなくても常時 Connected** で、Windows の既定で
  Wi-Fi より優先されることがある。この場合パケットは Hapbeat のいるネットワークに
  一生届かない。ケーブルを挿していないため症状がマルチホームに見えないのが厄介な点。
  - 探索（PING / CONNECT_STATUS）を**ローカルの各サブネット宛て**へ送るようにした。
    宛先は各 NIC の実マスクから `(ip & mask) | ~mask` で算出する（/16 は
    `x.y.255.255`、/25 は `x.y.z.127` になるため `.255` 決め打ちにはできない）。
    同一サブネットに NIC が 2 枚ある場合（Ethernet と Wi-Fi を同時に使うノート PC）は
    宛先で重複排除する。
  - **PONG が返ったサブネットに確定**し、以後の送信をそこへ向ける。設定項目は増やして
    いない（`connect(broadcast_addr=...)` を明示した場合はその宛先をそのまま使う）。
  - `255.255.255.255` は catch-all として常に候補に残す。SoftAP 構成、マスクを取得
    できないプラットフォーム、単一 NIC 環境は従来どおり動く。
  - **PLAY / STOP / STOP_ALL / STREAM_\* は従来どおり単一宛先**。ファームウェア
    v0.3.0 未満は PLAY/STOP の seq 重複排除を持たないため、再生コマンドを複数経路へ
    送ると触覚が 2 回鳴る。探索系と再生系で送信経路を分けている。
- **`hapbeat play` / `stop` / `stop-all` が常にブロードキャストになっていた**のを
  修正した。これらは `keepalive=False` で接続するため PING を撃たず、PONG が返らず、
  既知デバイスが 0 件のままだった。送信前に短い探索（最初の応答で打ち切り、上限
  0.4 秒）を行うようにした。ブロードキャストは AP が DTIM ビーコンまで保留するため、
  単発コマンドの発火遅れの原因になる。
- **送信エラーのログが埋め尽くされる問題**を修正した。宛先ごとに障害 1 回だけ警告し、
  復旧時に 1 行出す。ストリームは 1 秒あたり約 100 パケット送るため、抑制しないと
  現場調査に使えるログが残らない。探索の一部の宛先が失敗するのは正常な状態
  （Bluetooth PAN や Wi-Fi Direct のアダプタはブロードキャストを運べない）なので
  警告しない。全宛先が失敗した場合のみ警告する。

### 追加
- `hapbeat.netif.local_ipv4_subnets()` — ローカル IPv4 とプレフィックス長を返す。
  標準ライブラリのみ（Windows は `GetAdaptersAddresses`、Linux/macOS/BSD は
  `getifaddrs` を ctypes 経由で呼ぶ）。取得できないプラットフォームでは空を返し、
  呼び出し側は limited broadcast にフォールバックする。
- `Hapbeat.discover(..., stop_early=True)` — 最初の 1 台が応答した時点で返す。
  LAN の棚卸しではなく「次のパケットの宛先を決める」用途向け。

### 検証
- pytest 149 件（マルチホーム宛先算出・重複排除・探索と再生の経路分離・ログ抑制を含む）
- Windows 実機で `netif` の出力が `ifaddr` パッケージと完全一致することを確認
- **未検証**: Linux / macOS の `getifaddrs` 経路（実機なし）、Hapbeat 実機での疎通

## 0.2.0 — 送信をユニキャスト化

### 変更（既定の挙動）
- **PLAY / STOP / STOP_ALL とクリップストリームを、PING に応答した既知デバイスへ
  ユニキャスト送信**するようになりました（`connect(unicast=False)` で従来のブロード
  キャストに戻せます）。Wi-Fi AP は、同じ AP に省電力状態の端末が 1 台でもいると
  ブロードキャストフレームを次の DTIM ビーコン（100〜300 ms 周期）まで保留します。
  これが単発コマンドの発火遅れ、連続ストリームの周期的な途切れとして出ていました。
  デバイス側の設定では回避できない（原因は無関係な他端末）ため、送信側で対処します。
  Unity SDK で実測・検証済みの方針をそのまま移植したものです。
  - 応答があったデバイスが 1 台も無い間はブロードキャストで送ります（起動直後など）。
  - **ユニキャストとブロードキャストの二重送信はしません。**
  - `target` に一致しないデバイスは宛先から外します。アドレス未報告のデバイスは
    宛先に残します（デバイス側で同じ判定をするため、外すと取りこぼしになる）。
  - 既知デバイスがいて全て `target` 不一致だった場合、コマンドはブロードキャストに
    フォールバックします（キャッシュしたアドレスが古いときに STOP が消えて
    ループ再生が止まらなくなるのを防ぐため）。ストリームは送信しません
    （数百パケット分の電波を無駄にするため。取りこぼしても何も残らない）。
  - PING と CONNECT_STATUS はデバイス検出のためブロードキャストのままです。
- **キープアライブが PING も送るようになりました**（従来は CONNECT_STATUS のみ）。
  デバイスが PONG を返すのは PING に対してだけなので、これが無いとユニキャストの
  宛先表が `device_ttl` で空になり、以後ずっとブロードキャストに戻ってしまいます。
  `app_name` を渡していない接続でもキープアライブが動くようになりました。

### 追加
- `connect(unicast=..., device_ttl=...)` — ユニキャストの有効/無効と、最後の PONG から
  何秒間デバイスを宛先として保持するか（既定: キープアライブ間隔 × 3、最小 5 秒）。
- `protocol.address_matches(target, device_address)` — ファームウェアの
  `addressMatch()` と同一セマンティクスの判定関数。contracts §4.2 の例表を
  そのまま pytest に移植しています。
- `UdpClient.send_many(packet, addrs)`。
- `hapbeat` コマンドを実行したときに、新しい版が公開されていれば 1 行だけ知らせる
  ようになりました。同じ版については **24 時間に 1 回**まで（`hapbeat play` のような
  短命コマンドを連続実行してもうるさくならないように間引きます）。新しい版が出た
  場合は間隔を待たずに知らせます。
  - **`import hapbeat` では一切ネットワークにアクセスしません。** CI やオフライン
    環境でライブラリの import が外部通信するのは副作用として不適切なため、確認は
    CLI 実行時のみです。
  - コマンドの実行を待たせません。取得済みの情報が無い回は裏で取りに行くだけで、
    その回は何も出しません（次に CLI を使ったときに出ます）。
  - 無効化: `HAPBEAT_NO_UPDATE_CHECK=1`

### 修正
- **Windows で、応答しないデバイスへ送信した後に受信スレッドが停止し、以後デバイスを
  一切検出できなくなる問題**を修正しました。ICMP port unreachable が次の
  `recvfrom()` で `ConnectionResetError` として現れるため、`SIO_UDP_CONNRESET=False`
  で抑止し、受信ループも回復可能なエラーで停止しないようにしています。
  ユニキャストでは電源 OFF / 再起動中のデバイスに送ることが日常的に起きるため、
  この修正はユニキャスト化とセットで必須です（hapbeat-helper の同種修正と同じ対処）。

### 注意
- **部分ワイルドカードは使えません。** `*` はセグメント全体が `*` のときだけワイルド
  カードとして働きます（`player_1/pos_*` は不一致、`player_1/*` は一致）。
  contracts の例表に誤りがあったため、仕様側も併せて訂正しました。

## 0.1.0 — 初の公開リリース

Hapbeat 触覚デバイスを Wi-Fi UDP で駆動する Python SDK。コア機能は実行時依存ゼロ
（標準ライブラリの socket のみ）で、OSC ブリッジは任意の追加依存です。

### Fire API（level-1）
- `connect()` / `Hapbeat.play` / `stop` / `stop_all` / `ping` / `connect_status`。
- デバイス検出（PING/PONG）付きの UDP ブロードキャスト transport。
- 受信ソケットは**既定で ephemeral（OS 任せ）なローカルポートに bind** するため、
  UDP 7700 を占有する `hapbeat-helper` / Hapbeat Studio と共存できます。

### 触覚の編集側（EventMap）
- `EventMap.from_manifest` / `from_kit` / `from_dict` — kit manifest（schema 2.0.0）の
  `events`（command）と `stream_events`（clip）バケットを読み込みます。
- **Haptic file** `EventMap.from_file("haptics.json")` — kit を参照しつつ、イベント単位の
  `target` / `gain` を上乗せするオーバーレイ。呼び出し側が target を渡さなくても
  `play(id)` が解決します（Unity SDK の EventMap と同等）。

### command / clip 再生
- `play(id)` が manifest を見て自動分岐します。**command**（デバイスが導入済みクリップを再生）
  と **clip**（SDK が WAV を UDP でストリーミング）。
- `ClipStreamer` — ペーシングした STREAM_BEGIN/DATA/END（256 ms リング対応）、
  セッション単位で単一ストリーム。`play_clip` / `play_clip_file` / `stream_pcm`
  （アドホック PCM。例: ステレオの方向手がかり）/ `preload_clips`。
- 16 kHz モノラル PCM16 の WAV（実行時リサンプルなし。16 kHz 以外は警告）。

### ツール
- 汎用 **OSC ブリッジ**（`/hapbeat/*`）。`pip install "hapbeat-python-sdk[osc]"` で入る
  任意依存です。`hapbeat osc-bridge --haptics/--kit` が command/clip を振り分け、
  イベント単位の target を適用します。
- ブラウザ **launchpad**（`hapbeat launchpad`）— イベント / メトロノーム / 呼吸 /
  モールスを試せるローカル web ページ。各カードに等価な CLI コマンドを表示します。
- `hapbeat` CLI: `scan` / `play` / `stop` / `stop-all` / `ping` / `osc-bridge` /
  `launchpad`。

### サンプル
- `minimal`、`clip_project`（kit をプロジェクト内に同梱）、`osc_remote`（TouchOSC リモート）、
  `psychophysics_experiment`、`breathing_pacer`、`metronome`、`haptic_pad`、
  `task_notifier`、`morse_text` — いずれも単一ファイル・標準ライブラリのみ。

未実装（level-2）: clip のリアルタイム gain/pan バインドとマルチソースミキシング、
mDNS 検出、高レベルなトリガー抽象化。
