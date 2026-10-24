# Changelog

`hapbeat-python-sdk`（import 名: `hapbeat`）の主要な変更点をまとめます。

## Unreleased（1.0.0 に含める）

全製品の通信を 1.x にそろえる変更（DEC-096）。対応する contracts は 1.0.0（未 tag。`46f246e` 時点）。開発版は `1.0.0.dev1`。

### 追加
- 能力申告: 照合済みの PONG を初めて受けた機器、`firmware_version`・`device_boot_id` が変わった機器へ CAPS_REQ（0x12）を unicast で送り、CAPS（0x13）を `Device.caps`（`DeviceCaps`）に入れる。応答が無ければ 500 ms ごとに計 3 回送り、その後は `DeviceCaps.NONE`（能力申告なし）として、その boot の間は再要求しない。SDK の major（`protocol.SDK_MAJOR`）より新しい `generation` の機器を見つけたら、SDK の更新を促す警告を 1 回出す。
- ERROR（0xFF）の受信: 既知の機器の UDP 7700 から届き、stream の code は追跡中の session（今のもの・直前に終えたもの）の envelope と一致するものだけを、endpoint ごとに扱う。STREAM_LEASE_INVALID は lease を取り直して新しい世代で続きから、STREAM_SUPERSEDED は保留、STREAM_NOT_STARTED は同じ BEGIN を即時に再送、STREAM_TIMED_OUT は次の世代で続きから、STREAM_STOPPED はその機器から source を外す（同じ Playback は戻らない、他の機器では続く）、STREAM_TARGET_MISMATCH は endpoint を解決し直す、STREAM_GEN_UNSUPPORTED は分類し直す。CAPS_REQ への UNKNOWN_COMMAND は能力申告なし。
- 公開 API: `DeferredReason` に `UNKNOWN_CLASS`・`LEASE_UNAVAILABLE`・`SUPERSEDED` を追加（複数の機器で違うときは Superseded > LeaseUnavailable > UnknownClass > NoResolvedEndpoint）。`Device.stream_class`（`EndpointClass.GEN1 / GEN2 / UNKNOWN`）、`Hapbeat.stream_endpoints()`（`StreamEndpointInfo`）、`add_stream_endpoint_listener()` / `remove_stream_endpoint_listener()`、`LeaseState`、`DeviceCaps` を追加。
- Stream Gen 2 の BEGIN を最初の 3 つの DATA の前に再送し、session の間は 500 ms 以内ごとに再送する（Stream Gen 1 の機器には再送しない）。
- 自分の送信が 3 s 以上止まった後は、新しい世代で BEGIN してから DATA を送る。
- 差し替え（re-splice）: 送り済みのブロックの中に始まる source を足すとき、Stream Gen 2 の機器に 40 ms 以上たまっている見込みなら、今の世代を END して同じ lease の次の世代で作り直した混合をすぐに送る（新しい source が send-ahead の分だけ遅れない）。
- モーター 1 個の機器（CAPS の `haptic_channels` が 1）では、source の pan を適用せずに混合する。
- lease の世代を使い切ったら、新しい incarnation で lease を取り直す。

### 変更
- 探索・keep-alive の PING を 8 byte に戻し、lease を求める 16 byte の拡張 PING は stream する（する見込みの）機器へ unicast だけで送る。lease は TTL の 1/3（CAPS の `lease_ttl_s` / 3、無ければ 5 s）ごとに更新し、最後の session の後も 15 s 保つ。8 byte の PING への応答では機器の分類を変えない。keep-alive の間隔を Stream Gen 2 の機器のために縮める処理は削除した。
- PONG の lease tail を 1.x の規則で読む: 32 byte 以上・`HBS2`・extension_version 1 なら Stream Gen 2（先頭 32 byte だけ読み、reserved と未知の flag は見ない）、何も無ければ Stream Gen 1、それ以外は無視（`LEASE_TAIL_IGNORED`。機器ごとに 1 回警告）。
- superseded の保留は、`lease_reissued` の付いた有効な lease（機器が再発行した ticket）か `reacquire_stream_ownership()` でだけ解く。
- 同じ boot で ticket が替わった（lease の再発行・lease の取り直し）とき、有限の source を cursor から続ける（先頭から鳴らし直さない）。
- Stream Gen 1 の機器の 300 ms guard を待つ間の Playback は Deferred ではなく Active とする。
- 呼び方を contracts（versioning.md §4）に合わせた: `EndpointClass.V2 / LEGACY` → `GEN2 / GEN1`、`LEASE_TAIL_MALFORMED` → `LEASE_TAIL_IGNORED`、`V2_LEASE_REFRESH_INTERVAL` → `LEASE_REFRESH_INTERVAL`。参照先の `stream-session-v2.md` は `stream-session.md` に。

### 修正
- `parse_pong` が datagram 全体ではなく header の `payload_length` の範囲だけを payload として読む。

## 0.3.0 — stream session v2 対応・マルチホーム環境での修正

**ファーム 0.5.0 以上（stream session v2）の機器へ stream するには 0.3.0 以上が必要**（0.2.0 以前の stream はファーム 0.5.0 に捨てられ振動しない。旧ファームの機器へは従来どおり送れる）。最低版の一覧は contracts `specs/stream-session-v2.md` の Minimum versions 節。

### 追加
- WifiUdp streaming を `StreamHub` に統合した。複数の PCM / clip source を
  同時登録でき、同じ device endpoint では 16 kHz stereo PCM16 に混合し、異なる
  endpoint では独立した STREAM session を使用する。`stream_pcm` / clip API は
  source 単位の `StreamPlayback`（status / gain / pan / loop / stop）を返す。
- target 未解決時は `Deferred(NoResolvedEndpoint)` とし、STREAM packet を broadcast
  しない。PONG 後に有限 source を先頭から開始する。
- `play()` に `pan` 引数（-1 = 左 / 0 = 中央 / +1 = 右）を追加した。PLAY パケット
  末尾に f32 で常時付与する（contracts message-format.md §0x01 / DEC-055）。
  CLI `hapbeat play --pan`、OSC `/hapbeat/play` の第 5 引数からも指定できる。
- WifiUdp stream session v2（contracts stream-session-v2.md / DEC-074・075・081）に対応した。
  探索・keep-alive の PING を 16 byte の拡張 PING（client incarnation 付き）にし、
  その照合済み直接応答で機器ごとに v2 / 旧ファームを判定する。v2 機器には
  device 発行の lease と generation を BEGIN/DATA/END に付け、END 直後でも次の
  session を待たずに開始する（300 ms の END→BEGIN 待ちを撤廃）。旧ファームの機器には
  従来の v1 stream で送り、300 ms guard はその機器だけに掛ける。判定前・lease 無効・
  superseded の機器への stream は Deferred のまま送らない（superseded は
  `Hapbeat.reacquire_stream_ownership()` で新しい incarnation を作り再 PING して再取得）。
  判定・lease が変わった session は旧形式・旧 identity で END してから新形式で
  BEGIN する。PONG の volume_wiper 以降が完全な `HBS2` marker でない（"HB" 等の途中まで
  を含む）応答は旧ファームとして扱う。300 ms guard は endpoint（IP・port・address の組）
  単位で、同じ address の別機器には掛からない。keep-alive は全機器が旧ファームと判定される
  までは 5 s 以下の間隔で PING する（判定前に v2 lease が切れないように）。
  guard 待ちの endpoint が消えた・target に一致しなくなった有限 source は、その時点で
  完了する（機器復帰時に先頭から再生し直さない）。

### 修正
- `StreamHub` の mix: source の gain / pan を変えたとき、256 frame chunk の境界で段差が
  出ていた（zipper）。前回値から新しい値へ chunk 内で sample ごとに線形補間する。
- 複数 source の和がフルスケールを超えると hard clamp で歪んでいた。超えたときだけ
  0.9 FS へ gain を下げる limiter（chunk 内で補間、約 0.3 s で復帰）と、0.95 FS 以上の
  soft knee に置き換えた。ピークが 32768 以内の和（-32768 を含む full scale の clip 1 本
  など）は limiter が戻りきっていれば bit 単位でそのまま通す（0.95〜1.0 FS の block にも
  knee を掛けない。contracts §5.8）。
- 再サンプリング不要な PCM（16 kHz）の正規化が sample ごとの Python ループで、8.5 s の
  stereo で約 210 ms かかり GIL を握っていた（連続した `stream_pcm` の開始遅延・mixer の
  送出遅れ）。`array` によるコピーにして 5 ms 程度にした（結果は同一）。

### マルチホーム環境での探索・送信の修正

Unity SDK v0.4.0 で確定した接続まわりの修正を移植した。

#### 修正
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

#### 追加
- `hapbeat.netif.local_ipv4_subnets()` — ローカル IPv4 とプレフィックス長を返す。
  標準ライブラリのみ（Windows は `GetAdaptersAddresses`、Linux/macOS/BSD は
  `getifaddrs` を ctypes 経由で呼ぶ）。取得できないプラットフォームでは空を返し、
  呼び出し側は limited broadcast にフォールバックする。
- `Hapbeat.discover(..., stop_early=True)` — 最初の 1 台が応答した時点で返す。
  LAN の棚卸しではなく「次のパケットの宛先を決める」用途向け。

#### 検証
- pytest 149 件（マルチホーム宛先算出・重複排除・探索と再生の経路分離・ログ抑制を含む）
- Windows 実機で `netif` の出力が `ifaddr` パッケージと完全一致することを確認
- **Hapbeat 実機（duo_wl_v3 / fw 0.3.1）で疎通確認済み**:
  探索が全サブネット宛てにファンアウト → PONG でそのサブネットに確定 →
  PLAY / STOP がデバイスへユニキャスト**各 1 データグラム**（送信をソケット層で実測）。
  ブロードキャスト経路（`unicast=False`）でも宛先が確定サブネットになり、
  コマンド再生・クリップストリーミングとも実機が振動することを確認。
  `hapbeat play` も送信前に探索してユニキャストするようになった（従来は常にブロードキャスト）。
- **未検証**: Linux / macOS の `getifaddrs` 経路（実機なし）

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
- `StreamHub` — ペーシングした STREAM_BEGIN/DATA/END（256 ms リング対応）、
  endpoint 単位の multi-stream mixing。`play_clip` / `play_clip_file` / `stream_pcm`
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

未実装（level-2）: live push / mic capture、mDNS 検出、高レベルなトリガー抽象化。
