# APK v162 更新与 Web 适配

## 版本证据

`xbly.apk` 的 Manifest 与 DEX 均显示 `versionCode/versionName=162`（包名
`xin.banghua.beiyuan0`）。协议客户端的版本字段及 RoomKit User-Agent 已同步为
162。

## 新增聊天记录导出

DEX 中 `HistoryConversationActivity` 的导出流程调用：

```text
POST .../app/index.php?i=999999&c=entry&a=webapp&do=ExportChatRecord&m=socialchat
to_account=<对方账号>&page=1&page_size=100&export=1
```

响应是带可选 UTF-8 BOM 的 CSV，原生文件名格式为
`chat_export_<账号>_<时间戳>.csv`。Web 端对应提供：

- `POST /api/im/export`：校验会话权限、绑定对方 UID，转发固定字段并返回 CSV 下载；
- 聊天页“导出聊天记录”按钮：在上游不可用时导出当前已加载消息的 UTF-8 CSV。

## 举报接口

v162 增加 Moderation API 的 `submitReport` / `getMyReports` 模型。Web BFF 保留
`/api/social/report`，并增加兼容别名 `POST /api/report`；FastAPI 提供
`GET /api/report/my` 与 `GET /api/report/{id}` 的鉴权查询接口。
