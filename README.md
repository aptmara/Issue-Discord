-----

# Issues-Discord

DiscordでGitHub Issuesをシームレスに管理するためのBotです。タスクの起票からステータス更新、一覧表示まで、すべての操作をDiscord上からスラッシュコマンドで完結させることができます。

## 📝 概要

このBotは、GitHubリポジトリのIssueをDiscordチャンネルと連携させるための強力なツールです。主な機能として、チャンネルにタスク一覧を「バンドル」としてピン留め・定期更新する機能や、モーダルを利用した直感的なIssue作成機能を提供します。

開発チームが日常的に使用するDiscordを離れることなく、GitHub上のタスク管理を効率化することを目的としています。

-----

## ✨ 主な機能

  * **豊富なIssue操作コマンド**:

      * `/task_add`, `/task_add_modal`: テンプレートやモーダルを利用したIssue作成
      * `/task_assign`, `/task_claim`: 自分や他のメンバーへのタスク割り当て
      * `/task_done`, `/task_reopen`: Issueの完了・再オープン
      * `/task_comment`: Issueへのコメント追加
      * `/task_status`: ラベルごとの進捗サマリ表示
      * その他、担当解除 (`/task_unclaim`) やブロック解除 (`/task_unblock`) など多数

  * **タスク一覧のバンドル表示**:

      * 指定チャンネルに、複数のIssueグループ（セクション）をまとめたメッセージを自動生成・ピン留め
      * 更新間隔、ピン留め、リンクプレビューの有無を自由に設定可能
      * `status:todo` と `status:in_progress` のIssueを自動で分類・表示

  * **対話的なUIによる設定**:

      * `/task_groups_ui`: セレクトメニューとボタンで、タスク一覧のグループ（ラベルフィルタ）を対話的に編集・削除・リネーム可能

  * **入力の簡略化**:

      * ラベル入力時のオートコンプリート（`#bug` → `type:bug` のようなショートカットも完備）
      * 担当者入力時のコラボレーター名補完
      * `/link_github` でDiscordアカウントとGitHubアカウントを紐付け、`me` で自分を担当者に指定可能

  * **プリセット機能**:

      * よく使うラベルフィルタの組み合わせをプリセットとして保存し、簡単にグループを再作成

-----

## 🔧 動作環境とセットアップ

### 必要なもの

  * Python 3.8以上
  * 依存ライブラリ ( `requirements.txt` を参照)

### セットアップ手順

1.  **リポジトリのクローンまたはダウンロード**

    ```bash
    git clone https://github.com/your-repo/Issues-Discord.git
    cd Issues-Discord
    ```

2.  **依存ライブラリのインストール**

    ```bash
    pip install -r requirements.txt
    ```

3.  **環境変数の設定**
    以下の環境変数を設定してください。`.env` ファイルを作成して記述することも可能です。

      * `DISCORD_TOKEN`: **必須。** Discord Botのトークン
      * `GITHUB_TOKEN`: **必須。** GitHubのPersonal Access Token (`repo` スコープ権限が必要)
      * `GITHUB_OWNER`: **必須。** 対象リポジトリのオーナー名（ユーザーまたはOrganization）
      * `GITHUB_REPO`: **必須。** 対象リポジトリ名
      * `DISCORD_GUILD_ID`: (任意) コマンドを即時反映させたいDiscordサーバー（ギルド）のID

4.  **Botの実行**

    ```bash
    python bot.py
    ```

-----

## 🚀 コマンド一覧

### 👤 アカウント連携

| コマンド | 説明 |
| :--- | :--- |
| `/link_github <login>` | 自分のDiscordアカウントとGitHubアカウントを紐付けます。`me` の指定に必要です。 |

### 🎫 Issue操作

| コマンド | 説明 |
| :--- | :--- |
| `/task_add` | コマンド引数でIssueを素早く作成します。テンプレートも利用可能です。 |
| `/task_add_modal` | モーダルウィンドウを開き、対話形式でIssueを作成します。 |
| `/task_claim <number>` | 指定Issueの担当者を自分に設定し、ステータスを `in_progress` に変更します。 |
| `/task_unclaim <number>` | 指定Issueから自分の担当を外し、ステータスを `todo` に戻します。 |
| `/task_assign <number> <user>` | 指定Issueに担当者を割り当てます。 |
| `/task_done <number> [close]` | Issueを完了 (`status:done`) 扱いにします。オプションでCloseも可能です。 |
| `/task_reopen <number>` | Close済みのIssueを再度Openし、`status:todo` にします。 |
| `/task_unblock <number>` | Issueの `status:blocked` ラベルを解除します。 |
| `/task_comment <number> <comment>` | 指定Issueにコメントを投稿します。 |
| `/task_search [label] [keyword]` | ラベルやキーワードでIssueを検索します。 |
| `/task_status` | `todo`, `in_progress` などのステータスラベルごとのIssue数を表示します。 |
| `/task_list` | 簡易的なタスク一覧をEmbedで表示します。 |
| `/task_list_embed` | ボタンでページ操作が可能なタスク一覧をEmbedで表示します。 |

### 📦 バンドル・グループ管理

| コマンド | 説明 |
| :--- | :--- |
| `/task_bind_bundle` | コマンドを実行したチャンネルに、タスク一覧メッセージ（バンドル）を作成・ピン留めします。 |
| `/task_group_add` | バンドル内に表示するIssueのグループ（例: バグ一覧）を追加します。ラベルでフィルタ可能です。 |
| `/task_group_add_modal` | モーダルでグループを追加します。 |
| `/task_group_remove <name>` | バンドルから指定したグループを削除します。 |
| `/task_groups` | 現在のチャンネルのバンドル設定とグループ一覧を表示します。 |
| `/task_groups_edit` | バンドルの設定（更新間隔など）やグループ情報（名称、ラベル）を編集します。 |
| `/task_groups_ui` | **【推奨】** ボタンとセレクトメニューで、グループの追加・編集・削除などを直感的に行えるUIを呼び出します。 |

### ⚙️ プリセット

| コマンド | 説明 |
| :--- | :--- |
| `/task_preset_save` | よく使うラベルの組み合わせをプリセットとして保存します。 |
| `/task_group_add_preset` | 保存したプリセットを使って、素早くグループを追加します。 |

### 🔒 管理者向け

| コマンド | 説明 |
| :--- | :--- |
| `/admin_resync` | (管理者権限) アプリケーションコマンドをサーバーに再同期します。 |
