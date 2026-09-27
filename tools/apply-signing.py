#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jev Jarvis 签名配置套用工具。

读取仓库根目录的 signing.config，把签名信息写进工程各处，并生成编译用 workflow。

用法：
    python tools/apply-signing.py             # 套用并写文件
    python tools/apply-signing.py --dry-run   # 只打印会改什么，不写文件
    python tools/apply-signing.py --push      # 套用后 git add/commit/push（触发 Actions）

设计原则：所有替换都是「按结构」而不是「按旧值硬编码」，因此可以反复运行（幂等）。
"""

import argparse
import difflib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CONFIG_PATH = ROOT / "signing.config"
PBXPROJ = ROOT / "JevJarvis.xcodeproj" / "project.pbxproj"
PROJECT_YML = ROOT / "project.yml"
APP_ENTITLEMENTS = ROOT / "App" / "JevJarvis.entitlements"
KB_ENTITLEMENTS = ROOT / "Keyboard" / "JevKeyboard.entitlements"
JEV_MODEL = ROOT / "Shared" / "JevModel.swift"
WORKFLOW = ROOT / ".github" / "workflows" / "build-unsigned-ipa.yml"

PBXPROJ_KEYS = ("PRODUCT_BUNDLE_IDENTIFIER", "DEVELOPMENT_TEAM", "CODE_SIGN_STYLE",
                "CODE_SIGN_IDENTITY", "PROVISIONING_PROFILE_SPECIFIER")


# ---------------------------------------------------------------------------
# 读取配置
# ---------------------------------------------------------------------------

def parse_config(path):
    cfg = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        cfg[key] = value
    return cfg


ENV_OVERRIDES = {
    "MAIN_BUNDLE_ID": "JEV_MAIN_BUNDLE_ID",
    "TEAM_ID": "JEV_TEAM_ID",
    "APP_GROUP_ID": "JEV_APP_GROUP_ID",
    "CODE_SIGN_STYLE": "JEV_CODE_SIGN_STYLE",
    "CODE_SIGN_IDENTITY": "JEV_CODE_SIGN_IDENTITY",
    "PROFILE_NAME": "JEV_PROFILE_NAME",
    "PROFILE_NAME_KEYBOARD": "JEV_PROFILE_NAME_KEYBOARD",
}


def apply_env_overrides(cfg):
    """允许用环境变量临时覆盖配置（GitHub Actions 手动触发时用）。"""
    for key, env in ENV_OVERRIDES.items():
        value = os.environ.get(env, "").strip()
        if value:
            cfg[key] = value
    return cfg


def build_settings(cfg):
    main = cfg.get("MAIN_BUNDLE_ID", "").strip()
    suffix = cfg.get("KEYBOARD_BUNDLE_ID_SUFFIX", "").strip() or ".keyboard"
    return {
        "main": main,
        "keyboard": main + suffix,
        "prefix": ".".join(main.split(".")[:2]) if main else "",
        "team": cfg.get("TEAM_ID", "").strip(),
        "group": cfg.get("APP_GROUP_ID", "").strip(),
        "style": cfg.get("CODE_SIGN_STYLE", "").strip(),
        "identity": cfg.get("CODE_SIGN_IDENTITY", "").strip(),
        "profile": cfg.get("PROFILE_NAME", "").strip(),
        "profile_kb": (cfg.get("PROFILE_NAME_KEYBOARD", "").strip()
                       or cfg.get("PROFILE_NAME", "").strip()),
        "marketing": cfg.get("MARKETING_VERSION", "").strip(),
        "build": cfg.get("CURRENT_PROJECT_VERSION", "").strip(),
    }


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def yaml_scalar(value):
    """YAML 里需要引号的值就加引号。"""
    if value == "" or re.search(r"[\s:#]|^[&*!%@`\[\]{}>,|]", value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def read(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def set_pbx_key(body, key, value, indent):
    """在 pbxproj 的 buildSettings body 里更新或插入一个 KEY = value; 行。"""
    line = "%s%s = %s;" % (indent, key, value)
    rx = re.compile(r"(?m)^[ \t]*" + re.escape(key) + r" = [^\n]*;\s*$")
    if rx.search(body):
        return rx.sub(lambda m: line, body, count=1)
    anchor = re.search(r"(?m)^[ \t]*CODE_SIGN_ENTITLEMENTS = [^\n]*;\s*$", body)
    if anchor:
        return body[:anchor.end()] + "\n" + line + body[anchor.end():]
    return line + "\n" + body


# ---------------------------------------------------------------------------
# 各文件改写
# ---------------------------------------------------------------------------

def patch_pbxproj(text, s):
    block_re = re.compile(
        r"(?P<ind>[ \t]*)buildSettings = \{\n(?P<body>.*?)\n(?P<ind2>[ \t]*)\};", re.S)

    def repl(m):
        ind, body = m.group("ind"), m.group("body")
        settings_ind = ind + "\t"
        is_target = bool(re.search(r"(?m)^[ \t]*(INFOPLIST_FILE|CODE_SIGN_ENTITLEMENTS) =", body))
        is_kb = bool(re.search(r"[^\n]*Keyboard/", body)
                     or re.search(r"PRODUCT_BUNDLE_IDENTIFIER = [^;]*\.keyboard;", body))

        body = set_pbx_key(body, "DEVELOPMENT_TEAM", s["team"], settings_ind)
        if s["style"]:
            body = set_pbx_key(body, "CODE_SIGN_STYLE", s["style"], settings_ind)
        if s["marketing"] and "MARKETING_VERSION =" in body:
            body = set_pbx_key(body, "MARKETING_VERSION", s["marketing"], settings_ind)
        if s["build"] and "CURRENT_PROJECT_VERSION =" in body:
            body = set_pbx_key(body, "CURRENT_PROJECT_VERSION", s["build"], settings_ind)

        if is_target:
            body = set_pbx_key(body, "PRODUCT_BUNDLE_IDENTIFIER",
                               s["keyboard"] if is_kb else s["main"], settings_ind)
            if s["identity"]:
                body = set_pbx_key(body, "CODE_SIGN_IDENTITY", '"%s"' % s["identity"], settings_ind)
            if s["profile"]:
                profile = s["profile_kb"] if is_kb else s["profile"]
                body = set_pbx_key(body, "PROVISIONING_PROFILE_SPECIFIER",
                                   '"%s"' % profile, settings_ind)

        return ind + "buildSettings = {\n" + body + "\n" + m.group("ind2") + "};"

    text = block_re.sub(repl, text)
    if s["style"]:
        text = re.sub(r"(ProvisioningStyle = )\w+;",
                      lambda m: m.group(1) + s["style"] + ";", text)
    return text


def yml_target_set(text, target, key, value):
    """在 project.yml 的某个 target 的 settings.base 里更新或插入 key。"""
    header = re.search(r"(?m)^  " + re.escape(target) + r":[ \t]*$", text)
    if not header:
        return text
    start = header.end()
    nxt = re.search(r"(?m)^  \S.*:[ \t]*$", text[start:])
    end = start + nxt.start() if nxt else len(text)
    block = text[start:end]

    base = re.search(r"(?m)^(      base:[ \t]*\n)", block)
    if not base:
        return text
    after = block[base.end():]
    lines = after.splitlines(keepends=True)
    cut = 0
    for i, ln in enumerate(lines):
        if not ln.strip():
            cut = i + 1
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        if indent < 8:
            cut = i
            break
        cut = i + 1
    base_body = "".join(lines[:cut])
    rest = "".join(lines[cut:])

    desired = "        %s: %s\n" % (key, yaml_scalar(value))
    rx = re.compile(r"(?m)^        " + re.escape(key) + r":.*\n")
    if rx.search(base_body):
        base_body = rx.sub(desired, base_body, count=1)
    else:
        base_body = base_body + desired

    block = block[:base.end()] + base_body + rest
    return text[:start] + block + text[end:]


def patch_project_yml(text, s):
    text = re.sub(r"(?m)^(  bundleIdPrefix: ).*$", lambda m: m.group(1) + s["prefix"], text)
    text = re.sub(r"(?m)^(\s*DEVELOPMENT_TEAM: ).*$", lambda m: m.group(1) + s["team"], text)

    if s["style"]:
        if re.search(r"(?m)^\s*CODE_SIGN_STYLE:", text):
            text = re.sub(r"(?m)^(\s*CODE_SIGN_STYLE: ).*$",
                          lambda m: m.group(1) + s["style"], text)
        else:
            text = text.replace("settings:\n  base:\n",
                                "settings:\n  base:\n    CODE_SIGN_STYLE: %s\n" % s["style"], 1)

    def bundle(m):
        return m.group(1) + (s["keyboard"] if m.group(2).endswith(".keyboard") else s["main"])

    text = re.sub(r"(?m)^(\s*PRODUCT_BUNDLE_IDENTIFIER: )([^\s#]+)", bundle, text)
    text = re.sub(r"(?m)^(\s*- )group\.[A-Za-z0-9._-]+",
                  lambda m: m.group(1) + s["group"], text)

    if s["identity"]:
        text = yml_target_set(text, "JevJarvis", "CODE_SIGN_IDENTITY", s["identity"])
        text = yml_target_set(text, "JevKeyboard", "CODE_SIGN_IDENTITY", s["identity"])
    if s["profile"]:
        text = yml_target_set(text, "JevJarvis", "PROVISIONING_PROFILE_SPECIFIER", s["profile"])
        text = yml_target_set(text, "JevKeyboard", "PROVISIONING_PROFILE_SPECIFIER", s["profile_kb"])
    return text


def patch_entitlements(text, s):
    return re.sub(r"(<string>)group\.[^<]*(</string>)",
                  lambda m: m.group(1) + s["group"] + m.group(2), text)


def patch_swift(text, s):
    return re.sub(r'(appGroupID\s*=\s*")[^"]*(")',
                  lambda m: m.group(1) + s["group"] + m.group(2), text)


# ---------------------------------------------------------------------------
# 自动生成的编译 workflow
# ---------------------------------------------------------------------------

WORKFLOW_TEXT = """# 编译未签名 IPA（供全能签/自备证书重签）
# 触发：
#   1) 网页上修改 signing.config 并提交（push 到 main/master）→ 自动编译
#   2) Actions 页手动 Run workflow，可现场填 Bundle ID / Team / App Group 等
# 产物是未签名 IPA，重签由手机上的全能签用你自己的 p12 + 描述文件完成。
# 本文件由 tools/apply-signing.py 自动生成，请勿手改。
name: Build Unsigned IPA

on:
  push:
    branches: [main, master]
  workflow_dispatch:
    inputs:
      main_bundle_id:
        description: "主 App Bundle ID（留空则用仓库 signing.config）"
        required: false
      team_id:
        description: "Team ID"
        required: false
      app_group_id:
        description: "App Group"
        required: false
      code_sign_identity:
        description: "签名证书名，如 Apple Distribution"
        required: false
      profile_name:
        description: "主 App 描述文件名"
        required: false
      profile_name_keyboard:
        description: "键盘扩展描述文件名（留空则同主 App）"
        required: false

jobs:
  build-ipa:
    runs-on: macos-15
    timeout-minutes: 30
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Apply signing config
        env:
          JEV_MAIN_BUNDLE_ID: ${{ github.event.inputs.main_bundle_id }}
          JEV_TEAM_ID: ${{ github.event.inputs.team_id }}
          JEV_APP_GROUP_ID: ${{ github.event.inputs.app_group_id }}
          JEV_CODE_SIGN_IDENTITY: ${{ github.event.inputs.code_sign_identity }}
          JEV_PROFILE_NAME: ${{ github.event.inputs.profile_name }}
          JEV_PROFILE_NAME_KEYBOARD: ${{ github.event.inputs.profile_name_keyboard }}
        run: python3 tools/apply-signing.py

      - name: Show toolchain
        run: |
          xcodebuild -version
          swift --version

      - name: Build (Release, no signing)
        run: |
          xcodebuild \\
            -project JevJarvis.xcodeproj \\
            -target JevJarvis \\
            -configuration Release \\
            -sdk iphoneos \\
            -destination 'generic/platform=iOS' \\
            CODE_SIGNING_ALLOWED=NO \\
            build

      - name: Verify keyboard extension embedded
        run: |
          ls -la build/Release-iphoneos/JevJarvis.app/PlugIns/
          test -d build/Release-iphoneos/JevJarvis.app/PlugIns/JevKeyboard.appex

      - name: Package unsigned IPA
        run: |
          mkdir -p Payload
          cp -R build/Release-iphoneos/JevJarvis.app Payload/
          zip -qr JevJarvis-unsigned.ipa Payload
          ls -la JevJarvis-unsigned.ipa

      - name: Upload artifact
        uses: actions/upload-artifact@v4
        with:
          name: JevJarvis-unsigned-ipa
          path: JevJarvis-unsigned.ipa
"""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="套用 signing.config 到工程各处")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写文件")
    ap.add_argument("--push", action="store_true", help="套用后 git add/commit/push")
    args = ap.parse_args()

    if not CONFIG_PATH.exists():
        sys.exit("找不到 signing.config（应在仓库根目录）")

    cfg = parse_config(CONFIG_PATH)
    apply_env_overrides(cfg)
    s = build_settings(cfg)

    missing = [k for k in ("main", "team", "group") if not s[k]]
    if missing:
        sys.exit("signing.config 缺少必填项：%s" % ", ".join(missing))

    if s["profile"] and not s["identity"]:
        print("⚠️  填了 PROFILE_NAME 但没填 CODE_SIGN_IDENTITY，手动签名可能失败。")

    tasks = [
        (PROJECT_YML, patch_project_yml),
        (PBXPROJ, patch_pbxproj),
        (APP_ENTITLEMENTS, patch_entitlements),
        (KB_ENTITLEMENTS, patch_entitlements),
        (JEV_MODEL, patch_swift),
    ]

    print("=== 套用签名配置 ===")
    print("  Bundle ID : %s  /  %s" % (s["main"], s["keyboard"]))
    print("  Team      : %s" % s["team"])
    print("  App Group : %s" % s["group"])
    print("  签名方式  : %s" % (s["style"] or "(不改)"))
    print("  证书      : %s" % (s["identity"] or "(不改)"))
    print("  描述文件  : %s" % (s["profile"] or "(不改)"))
    print()

    changed = 0
    for path, patch in tasks:
        if not path.exists():
            print("[缺失] %s" % path.relative_to(ROOT))
            continue
        raw = read(path)
        crlf = "\r\n" in raw
        # 统一按 LF 做替换，再还原原换行风格，避免把 CRLF 文件改成混合换行
        text = raw.replace("\r\n", "\n")
        new = patch(text, s)
        if crlf:
            new = new.replace("\n", "\r\n")
        rel = path.relative_to(ROOT)
        if raw == new:
            print("[跳过] %s（无变化）" % rel)
            continue
        changed += 1
        if args.dry_run:
            print("[将改] %s" % rel)
            diff = difflib.unified_diff(text.splitlines(), new.replace("\r\n", "\n").splitlines(),
                                        lineterm="", n=1)
            for line in diff:
                if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                    print("       " + line)
        else:
            write(path, new)
            print("[改写] %s" % rel)

    # workflow：内容固定，始终保证与脚本一致
    old_wf = read(WORKFLOW) if WORKFLOW.exists() else None
    if old_wf != WORKFLOW_TEXT:
        changed += 1
        if args.dry_run:
            print("[将改] %s" % WORKFLOW.relative_to(ROOT))
        else:
            write(WORKFLOW, WORKFLOW_TEXT)
            print("[写入] %s" % WORKFLOW.relative_to(ROOT))
    else:
        print("[跳过] %s（无变化）" % WORKFLOW.relative_to(ROOT))

    print()
    if args.dry_run:
        print("预览完成：%d 个文件会有变化。去掉 --dry-run 即写入。" % changed)
        return

    print("完成：%d 个文件已更新。" % changed)

    if args.push:
        if changed == 0:
            print("没有变化，跳过 push。")
            return
        managed = [
            "signing.config", "project.yml",
            "JevJarvis.xcodeproj/project.pbxproj",
            "App/JevJarvis.entitlements", "Keyboard/JevKeyboard.entitlements",
            "Shared/JevModel.swift",
            ".github/workflows/build-unsigned-ipa.yml",
        ]
        subprocess.run(["git", "-C", str(ROOT), "add", "--"] + managed, check=True)
        subprocess.run(["git", "-C", str(ROOT), "commit", "-m", "chore: apply signing config"],
                       check=True)
        subprocess.run(["git", "-C", str(ROOT), "push"], check=True)
        print("已推送，去 GitHub Actions 查看编译。")


if __name__ == "__main__":
    main()
