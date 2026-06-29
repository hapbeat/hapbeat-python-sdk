# Changelog

`hapbeat-python-sdk`（import 名: `hapbeat`）の主要な変更点をまとめます。

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
