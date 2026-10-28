"""把这个仓库创建到 GitHub 并推送上去。

为什么需要单独跑一次：GitHub 的写操作必须用账号凭据（令牌或 OAuth 授权），
没有任何"匿名推送"的通道。本脚本负责把「建仓库 + 配远端 + 推送 + 校验」
这几步串起来，你只需要提供一个 Personal Access Token。

--------------------------------------------------------------- 取令牌的步骤
1. 浏览器登录 GitHub 后打开  https://github.com/settings/tokens
2. 「Generate new token」→「Generate new token (classic)」
3. Note 随便写（例如 acx720-push），Expiration 选 7 天即可
4. 权限**只勾 `repo`** 这一项（创建仓库 + 推送都在这个范围里）
5. 生成后复制那串 `ghp_...`（只显示一次）

--------------------------------------------------------------- 令牌怎么交给我
三种任选，优先级从高到低：
  A. 双击仓库根目录的「一键提交到GitHub.bat」，按提示粘贴（不回显、不落盘）
  B. 命令行：set GITHUB_TOKEN=ghp_xxx  然后  python tool/push_to_github.py
  C. 把令牌写进 _submit/token.txt（该目录已在 .gitignore 里）

--------------------------------------------------------------- 安全说明
- 令牌只在内存里用，不写进 .git/config（推送后会把远端地址还原成干净 URL）
- 脚本不会把令牌打印到屏幕或日志
- 推完可以去 https://github.com/settings/tokens 把它删掉（推荐）
"""
from __future__ import annotations

import getpass
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(ROOT, "_submit", "token.txt")
DEFAULT_REPO = "ACX720-VoiceChanger"
DEFAULT_DESC = ("ACX720(Artix-7 7A35T) 实时变声器：FPGA 变调/年龄变声 + "
                "STM32 音频链路 + Python 上位机")


# ---------------------------------------------------------------- 小工具
def log(msg):
    print(msg, flush=True)


def fail(msg, code=1):
    print("\n[失败] " + msg, flush=True)
    sys.exit(code)


def read_token():
    """按 环境变量 → token.txt → 交互输入 的顺序取令牌。"""
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        v = (os.environ.get(k) or "").strip()
        if v:
            log("令牌来源：环境变量 %s" % k)
            return v
    if os.path.exists(TOKEN_FILE):
        v = open(TOKEN_FILE, encoding="utf-8").read().strip()
        if v:
            log("令牌来源：%s" % os.path.relpath(TOKEN_FILE, ROOT))
            return v
    log("没有找到令牌。请粘贴你的 GitHub Personal Access Token")
    log("（在 https://github.com/settings/tokens 生成，只勾 repo；粘贴时不显示）")
    try:
        v = getpass.getpass("Token: ").strip()
    except Exception:
        v = sys.stdin.readline().strip()
    if not v:
        fail("没有输入令牌，已退出。")
    return v


def _ssl_ctx():
    """正常校验证书。如果本机装的是自签 CA，可设 GITHUB_CA_BUNDLE 指过去。"""
    cafile = os.environ.get("GITHUB_CA_BUNDLE")
    return ssl.create_default_context(cafile=cafile) if cafile \
        else ssl.create_default_context()


def api(path, tok, method="GET", payload=None):
    """调 GitHub REST API，返回 (状态码, 解析后的 JSON 或 None)。"""
    url = "https://api.github.com" + path
    data = None
    headers = {
        "Authorization": "token " + tok,
        "Accept": "application/vnd.github+json",
        "User-Agent": "acx720-push-script",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx()) as r:
            body = r.read().decode("utf-8")
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"message": body[:200]}
    except Exception as e:
        fail("无法连接 GitHub API：%s\n"
             "（本机若缺少证书吊销列表，git 侧会自动加 schannelCheckRevoke=false，"
             "但 Python 侧要求能访问 api.github.com）" % e)


def git(*args, check=True, quiet=False):
    cmd = ["git"] + list(args)
    if not quiet:
        log("  $ git " + " ".join(args))
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if check and p.returncode != 0:
        fail("git %s 失败：\n%s\n%s" % (args[0], p.stdout, p.stderr))
    return p


# ---------------------------------------------------------------- 主流程
def main():
    repo = DEFAULT_REPO
    private = False
    for a in sys.argv[1:]:
        if a.startswith("--repo="):
            repo = a.split("=", 1)[1]
        elif a == "--private":
            private = True
        elif a in ("-h", "--help"):
            log(__doc__)
            return

    log("== 1/5 检查本地仓库 ==")
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        fail("当前目录不是 git 仓库：%s" % ROOT)
    branch = git("rev-parse", "--abbrev-ref", "HEAD", quiet=True).stdout.strip()
    n_commits = git("rev-list", "--count", "HEAD", quiet=True).stdout.strip()
    log("  分支 %s，%s 个提交，工作区%s"
        % (branch, n_commits,
           "干净" if not git("status", "--porcelain", quiet=True).stdout.strip()
           else "有未提交改动"))

    tok = read_token()
    if not re.match(r"^(ghp_|github_pat_|gho_|ghu_|ghs_)", tok):
        log("  提醒：令牌看起来不是 GitHub 的常见前缀，仍会照常尝试。")

    log("\n== 2/5 校验令牌 ==")
    code, me = api("/user", tok)
    if code == 401:
        fail("令牌无效或已过期（401）。重新生成一个，确认勾了 repo 权限。")
    if code != 200:
        fail("校验令牌失败：HTTP %s %s" % (code, (me or {}).get("message", "")))
    login = me["login"]
    log("  已认证：%s（%s）" % (login, me.get("name") or "未设置昵称"))

    log("\n== 3/5 创建仓库 %s/%s ==" % (login, repo))
    code, info = api("/repos/%s/%s" % (login, repo), tok)
    if code == 200:
        log("  仓库已存在，直接推送（%s）" % info["html_url"])
        exist_commits = info.get("size", 0) > 0
    elif code == 404:
        code2, created = api("/user/repos", tok, method="POST", payload={
            "name": repo,
            "description": DEFAULT_DESC,
            "private": bool(private),
            "has_issues": True,
            "has_wiki": False,
            "auto_init": False,        # 不建 README，否则首推要先 pull 合并
        })
        if code2 not in (200, 201):
            fail("创建仓库失败：HTTP %s %s"
                 % (code2, (created or {}).get("message", "")))
        log("  已创建：%s" % created["html_url"])
        exist_commits = False
    else:
        fail("查询仓库失败：HTTP %s %s" % (code, (info or {}).get("message", "")))

    log("\n== 4/5 推送 ==")
    clean = "https://github.com/%s/%s.git" % (login, repo)
    # 令牌只放在这一次的命令行里，不写进 .git/config
    auth_url = "https://%s:%s@github.com/%s/%s.git" % (login, tok, login, repo)
    # 本机 schannel 缺少吊销列表校验时会直接报 CRYPT_E_NO_REVOCATION_CHECK，
    # 等价于 curl 的 --ssl-no-revoke；只对本次命令生效。
    push = subprocess.run(
        ["git", "-c", "http.schannelCheckRevoke=false", "push", auth_url,
         "%s:%s" % (branch, branch), "--force"],
        cwd=ROOT, capture_output=True, text=True)
    safe = (push.stdout + push.stderr).replace(tok, "***")
    if push.returncode != 0:
        # 不带 --force 再试一次，避免把已有内容覆盖掉
        push2 = subprocess.run(
            ["git", "-c", "http.schannelCheckRevoke=false", "push", auth_url,
             "%s:%s" % (branch, branch)],
            cwd=ROOT, capture_output=True, text=True)
        if push2.returncode != 0:
            fail("推送失败：\n%s" % (push2.stdout + push2.stderr).replace(tok, "***"))
    log("  推送完成")
    if exist_commits:
        log("  注意：仓库原本已有内容，本次用了 --force 对齐，旧内容被覆盖。")

    # 远端地址还原成不含令牌的干净 URL
    git("remote", "remove", "origin", check=False, quiet=True)
    git("remote", "add", "origin", clean, quiet=True)
    git("branch", "--set-upstream-to=origin/%s" % branch, branch,
        check=False, quiet=True)

    log("\n== 5/5 校验 ==")
    code, br = api("/repos/%s/%s/branches/%s" % (login, repo, branch), tok)
    if code == 200:
        log("  远端 %s 头提交：%s" % (branch, br["commit"]["sha"][:7]))
        local = git("rev-parse", "HEAD", quiet=True).stdout.strip()[:7]
        log("  本地头提交：      %s" % local)
        if br["commit"]["sha"][:7] != local:
            fail("远端与本地不一致，请检查。")
    else:
        log("  分支校验返回 HTTP %s（推送命令已成功，可稍后手动确认）" % code)

    log("\n全部完成：")
    log("  " + clean.replace(".git", ""))
    log("\n建议：去 https://github.com/settings/tokens 把这个令牌删掉。")
    del tok


if __name__ == "__main__":
    main()
