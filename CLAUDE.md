# CLAUDE.md — hapbeat-python-sdk

## repo の目的

Python 向け SDK。`import hapbeat` で Hapbeat デバイスを Wi-Fi UDP ブロードキャストで駆動する。
研究者（PsychoPy / Jupyter / ROS）・メディアアーティスト・プロトタイピングが主対象。

`hapbeat-vrchat` / `hapbeat-touchdesigner` の **土台**でもある（両者は本 SDK に相乗りする）。

## 全体アーキテクチャ上の役割

contracts の Layer 1 UDP/OSC 仕様の上に薄く載る code SDK。Unity SDK と同じ
「起点(fire) ↔ 調整(EventMap) を互いに素に分け、event id で紐づける」設計を踏襲する。

## 責務

- Layer 1 protocol の Python 実装（`protocol.py` — contracts/message-format.md の wire 仕様に byte 単位で追従）
- Wi-Fi UDP 直接通信（broadcast）+ デバイス検出（PING/PONG）
- `play / stop / stop_all / ping / connect_status` の fire API（`hapbeat.py`）
- `EventMap`（kit manifest schema 2.0.0 から default gain 解決 = 調整側）
- 汎用 OSC bridge（`/hapbeat/*` を中継、`osc.py`、optional dep python-osc）
- `hapbeat` CLI

## 管理対象

- Python パッケージ `src/hapbeat/`
- pytest（contract round-trip）
- examples / docs

## 管理対象外

- Bridge サーバ実装 / ファームウェア / Kit ビルドツール本体
- VRChat 固有スキーマのマッピング（→ hapbeat-vrchat）
- TouchDesigner 固有の .tox / ノード（→ hapbeat-touchdesigner）

## 依存関係

### 依存してよい repo

- hapbeat-contracts（仕様のみ）

### 依存される repo

- hapbeat-vrchat
- hapbeat-touchdesigner（埋め込み Python から import）

## 壊してはいけない公開インターフェース

- `hapbeat.connect()` / `Hapbeat.play(event_id, gain)` 等の公開 API
- `protocol.py` の wire 出力（firmware が受理する byte 列）

## やってはいけないこと

- 独自プロトコルを作る（contracts に従う）
- 後方互換コード（旧 command 名 alias 等）を作る — リリース前のため不要
- wire 仕様を Python の都合で歪める

## まだ作らないもの（level-2 以降）

- 高レベル trigger 抽象（衝突/状態/連続値 → 自動 fire）。Unity の CollisionTrigger 等に相当
- streaming clip 再生のリアルタイム gain/pan binding
- mDNS（zeroconf）検出（現状は broadcast PING のみ）

## 設計メモ

- **wire 互換の正**は firmware が受理する byte 列。`HapbeatProtocol.cs` と
  `hapbeat-helper/protocol.py` が実機実績のある参照実装。
- **CONNECT_STATUS の byte 順**は spec doc §0x20 ではなく `HapbeatProtocol.cs`
  実装（`connected, group, appName, deviceName`）に合わせている。spec doc が古い。
  → contracts 側 cleanup を別途起票検討。

## テスト

```bash
python -m pytest          # contract round-trip（wire layout 固定）
```

## オフライン動作

クラウド不要。LAN 上にデバイスがいれば動作。

## 重要な概念

- **Event ID** — kit 内のイベント識別子。`play(event_id)` で再生指示。
- **EventMap** — event id → default gain（kit manifest の intensity）の調整カタログ。fire と直交。
- **Discovery** — broadcast PING に対する PONG でデバイス検出。

## 指示書

- `instructions/` — 他セッションからの未実行指示書 / `completed/` / `applied/`（横断編集の事後承認 note）
- セッション開始時に `instructions/` を確認し、該当があれば適用する。

## エージェント共通メモリ

- セッション間で引き継ぐ知見は workspace ルートの `docs/claude-memory/` に保存（INDEX.md 更新）。
- この repo からの相対パスは `../docs/claude-memory/`。
