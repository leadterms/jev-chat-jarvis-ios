# 从描述文件提取签名配置字段

本文说明：填写仓库根目录的 `signing.config` 时，`MAIN_BUNDLE_ID`、`APP_GROUP_ID`、`TEAM_ID`、`PROFILE_NAME` 这几个值该从哪里拿。

## 需要哪个文件

需要的是 **描述文件 `.mobileprovision`**（不是 `.p12` 证书）。

- `.p12` **不需要**：它只用于签名，里面没有 Bundle ID / App Group 这些信息。
- 主 App 与键盘扩展通常**各有一份**描述文件，两份都要看。

## 字段对照

| signing.config 配置项 | 取自描述文件里的字段 |
|---|---|
| `MAIN_BUNDLE_ID` | `Entitlements → application-identifier`（去掉开头的 `TEAMID.`） |
| `APP_GROUP_ID` | `Entitlements → com.apple.security.application-groups`（可能有多个，一般取第一个） |
| `TEAM_ID` | `TeamIdentifier` |
| `PROFILE_NAME` | `Name`（用这个名字，不是文件名） |
| `PROFILE_NAME_KEYBOARD` | 键盘扩展那份描述文件的 `Name` |

主 App 的 `application-identifier` 形如 `ABCDE12345.com.example.jev`；
键盘扩展的形如 `ABCDE12345.com.example.jev.keyboard`。

所以：

- `MAIN_BUNDLE_ID` / `APP_GROUP_ID` / `PROFILE_NAME` / `TEAM_ID` 取自主 App 那份；
- `PROFILE_NAME_KEYBOARD` 取自键盘扩展那份；
- `APP_GROUP_ID` 两份里应当一致。

## 示例

主 App 描述文件里的字段：

```
Name                  = Example AdHoc Profile
TeamIdentifier        = ABCDE12345
application-identifier = ABCDE12345.com.example.jev
application-groups    = [ "group.com.example.jev" ]
```

对应填进 `signing.config`：

```
MAIN_BUNDLE_ID=com.example.jev
TEAM_ID=ABCDE12345
APP_GROUP_ID=group.com.example.jev
PROFILE_NAME=Example AdHoc Profile
```

键盘扩展描述文件里的字段：

```
Name                   = Example Keyboard AdHoc Profile
application-identifier = ABCDE12345.com.example.jev.keyboard
```

对应填进 `signing.config`：

```
PROFILE_NAME_KEYBOARD=Example Keyboard AdHoc Profile
```

## 查看描述文件内容

描述文件是 PKCS#7 签名的 plist，展开即可看到上面这些字段。常见做法：

- macOS：`security cms -D -i <文件>.mobileprovision`
- 其它平台：用任意 PKCS#7 / plist 解析方式取出其中的 XML plist 即可（字段名同上表）。

## 关于有效期

描述文件**过期不影响提取这些字段**，字段照样能读出来。
但过期的描述文件无法用于最终签名，重签时仍会失败，需要先续期或重新下发。

## 填完之后

在 GitHub 网页上编辑并提交 `signing.config`，`Build Unsigned IPA` workflow 会自动触发，在云端套用配置并编出未签名 IPA。
