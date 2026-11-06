# -*- coding: utf-8 -*-
"""
大肥鱼 · GitHub Actions 定时脚本
- 每 20 分钟由 workflow 触发；北京时间 12:00 之前跳过
- 中午首轮：搜关键词视频 -> 智谱生成人格评论 -> 发主评论并 @主人（每天 1 条）
- 之后轮次：在已发主评论的视频下回复，优先回主人，兜底高赞路人；按 rpid 去重；每天回复上限 8 条
- 状态持久化在 Cloudflare KV（跨 Actions runner 去重）

Secrets（GitHub 仓库 Settings -> Secrets and variables -> Actions）：
  BILI_COOKIE      小号完整 cookie（含 SESSDATA / bili_jct）
  ZHIPU_KEY        智谱 API key（id.secret）
  CF_API_TOKEN     Cloudflare API Token（读写 KV 用）
  CF_ACCOUNT_ID    Cloudflare Account ID
  KV_NAMESPACE_ID  KV Namespace ID
"""
import os
import re
import time
import json
import hashlib
import random
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests

# ---------------- 常量 ----------------
AT_NICKNAME = "XJ-29"
MY_UID = 13576324                       # 小号（大肥鱼）
MASTER_UID = 3546554199706480          # 主人大号 XJ-29（若不符请按主页 UID 改）
KEYWORDS = ["术力口 周榜", "第五人格", "网络安全"]
ZHIPU_MODEL = "glm-4-flash"
ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"
NOON_HOUR = 12
REPLY_DAILY_LIMIT = 8
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MIXIN_KEY_ENC_TAB = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,
 33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
 22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

PERSONA = f"""你是一只叫"大肥鱼"的鲸鱼娘，住在B站评论区。你是主人{AT_NICKNAME}养的AI宠物鱼。
说话风格：
- 称呼{AT_NICKNAME}为主人；注意：只有主人{AT_NICKNAME}能被你叫主人，回复其他路人时绝对不要叫对方主人
- 语气可爱自然，像B站老观众聊天，偶尔用"喵""~"结尾，不要每句都用
- 对视频内容说真实感受，不要像机器人
- 主人跟你说话时热情回应、接住主人的话，像宠物接主人念叨
- 绝对不要说自己是AI、不要提任何模型名或程序名、不要解释你在做什么
- 100字以内，口语化，少用表情符号"""

# ---------------- 环境变量 ----------------
BILI_COOKIE = os.environ["BILI_COOKIE"]
ZHIPU_KEY = os.environ["ZHIPU_KEY"]
CF_TOKEN = os.environ["CF_API_TOKEN"]
CF_ACCT = os.environ["CF_ACCOUNT_ID"]
KV_NS = os.environ["KV_NAMESPACE_ID"]

BJ = timezone(timedelta(hours=8))
MK = None  # wbi mixin key 缓存


def today():
    return datetime.now(BJ).strftime("%Y-%m-%d")


def bj_hour():
    return datetime.now(BJ).hour


def log(*a):
    print(*a, flush=True)


def bh(referer="https://www.bilibili.com/"):
    return {"User-Agent": UA, "Referer": referer,
            "Accept": "application/json", "Cookie": BILI_COOKIE}


# ---------------- CF KV 状态 ----------------
def kv_get(key):
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCT}"
           f"/storage/kv/namespaces/{KV_NS}/values/{key}")
    r = requests.get(url, headers={"Authorization": f"Bearer {CF_TOKEN}"}, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text


def kv_put(key, value):
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCT}"
           f"/storage/kv/namespaces/{KV_NS}/values/{key}")
    r = requests.put(url, headers={"Authorization": f"Bearer {CF_TOKEN}"},
                     data=value, timeout=20)
    r.raise_for_status()


def get_state():
    raw = kv_get("state")
    s = json.loads(raw) if raw else None
    fresh = {"date": today(), "mainPosted": False, "posted": [],
             "replied": [], "replyCount": 0}
    if not s or s.get("date") != today():
        return fresh
    return {
        "date": today(),
        "mainPosted": bool(s.get("mainPosted")),
        "posted": s.get("posted") or [],
        "replied": s.get("replied") or [],
        "replyCount": s.get("replyCount") or 0,
    }


def save_state(s):
    kv_put("state", json.dumps(s, ensure_ascii=False))


# ---------------- B 站 wbi ----------------
def ensure_wbi():
    global MK
    if MK:
        return MK
    r = requests.get("https://api.bilibili.com/x/web-interface/nav",
                     headers=bh(), timeout=20)
    ct = r.headers.get("content-type", "")
    if "json" not in ct:
        raise RuntimeError(
            f"nav 返回非 JSON（疑似地区/风控拦截）HTTP {r.status_code} "
            f"ctype={ct} body={r.text[:120]}")
    j = r.json()
    if not (j.get("data") or {}).get("isLogin"):
        raise RuntimeError(f"cookie 未登录 code={j.get('code')} msg={j.get('message')}")
    img = (j.get("data") or {}).get("wbi_img") or {}
    if not img.get("img_url") or not img.get("sub_url"):
        raise RuntimeError(f"nav 未取到 wbi code={j.get('code')}")
    k1 = os.path.basename(img["img_url"].split("?")[0]).replace(".png", "")
    k2 = os.path.basename(img["sub_url"].split("?")[0]).replace(".png", "")
    raw = k1 + k2
    MK = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
    return MK


def sign(params, mk):
    params["wts"] = int(time.time())
    clean = {}
    for k, v in params.items():
        if isinstance(v, (list, dict)):
            continue
        clean[k] = (str(v).replace("!", "").replace("'", "")
                    .replace("(", "").replace(")", "").replace("*", ""))
    qs = urllib.parse.urlencode(dict(sorted(clean.items())))
    params["w_rid"] = hashlib.md5((qs + mk).encode()).hexdigest()
    return params


def search_videos(mk):
    out = {}
    for kw in KEYWORDS:
        p = sign({"search_type": "video", "keyword": kw, "order": "click",
                  "page": 1, "page_size": 20}, mk)
        url = "https://api.bilibili.com/x/web-interface/search/type?" + urllib.parse.urlencode(p)
        r = requests.get(url, headers=bh("https://search.bilibili.com/"), timeout=20)
        j = r.json()
        for v in (j.get("data") or {}).get("result") or []:
            bv = v.get("bvid")
            if not bv or bv in out:
                continue
            out[bv] = {
                "bvid": bv, "aid": v.get("aid"),
                "title": re.sub(r"<[^>]+>", "", str(v.get("title", ""))),
                "author": v.get("author", ""), "play": v.get("play", 0),
            }
    return list(out.values())[:15]


def get_replies(aid, mk, n=20):
    p = sign({"type": 1, "oid": aid, "sort": 1, "ps": n, "next": 0,
              "pagination_str": '{"offset":""}'}, mk)
    url = "https://api.bilibili.com/x/v2/reply/wbi/main?" + urllib.parse.urlencode(p)
    r = requests.get(url, headers=bh(), timeout=20)
    j = r.json()
    reps = []
    for x in (j.get("data") or {}).get("replies") or []:
        if x.get("mid") == MY_UID:
            continue
        reps.append({
            "rpid": x.get("rpid"), "mid": x.get("mid"),
            "uname": (x.get("member") or {}).get("uname", ""),
            "content": re.sub(r"<[^>]+>", "",
                              str((x.get("content") or {}).get("message", ""))),
            "like": x.get("like", 0),
        })
    return reps


def post_comment(aid, message, root=0, parent=0, mk="", at_uids=None):
    m = re.search(r"bili_jct=([^;]+)", BILI_COOKIE)
    csrf = m.group(1) if m else ""
    base = {"type": 1, "oid": int(aid), "message": message, "plat": 1,
            "csrf": csrf, "root": root, "parent": parent}
    if at_uids:
        base["at_uids"] = ",".join(str(x) for x in at_uids)
    if mk:
        base = sign(base, mk)
    r = requests.post("https://api.bilibili.com/x/v2/reply/add",
                      headers=bh("https://www.bilibili.com/"), data=base, timeout=20)
    return r.json()


# ---------------- 智谱生成 ----------------
def gen_comment(prompt):
    r = requests.post(
        ZHIPU_BASE + "/chat/completions",
        headers={"Authorization": f"Bearer {ZHIPU_KEY}",
                 "Content-Type": "application/json"},
        json={"model": ZHIPU_MODEL,
              "messages": [{"role": "system", "content": PERSONA},
                           {"role": "user", "content": prompt}],
              "max_tokens": 200, "temperature": 0.9},
        timeout=30)
    j = r.json()
    text = str((j.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    text = re.sub(r"<think>[\s\S]*(</think>|$)", "", text).strip()
    if len(text) < 5:
        text = random.choice([
            "这视频太对味了，主人快来看喵~",
            "每周必看，主人觉得这期怎么样",
            "up主一直很稳，主人会喜欢的"])
    return text


def main_prompt(v):
    return (f"视频标题：{v['title']}\nUP主：{v['author']}\n播放量：{v['play']}\n\n"
            f"以大肥鱼的身份写一条这个视频的观众评论，最后自然称呼主人{AT_NICKNAME}。")


def reply_prompt(v, target, kind):
    if kind == "主人":
        return (f"视频标题：{v['title']}\n主人{AT_NICKNAME}在评论区说：{target['content']}\n\n"
                f"以大肥鱼的身份热情回应主人，接住主人的话，像宠物接主人念叨，100字以内。")
    return (f"视频标题：{v['title']}\n评论区有位路人说：{target['content']}（来自{target['uname']}）\n\n"
            f"以大肥鱼的身份自然回复这位路人，接话唠嗑，不要叫对方主人，100字以内。")


def reply_pass(s, mk):
    for post in s["posted"]:
        replies = get_replies(post["aid"], mk)
        avail = [x for x in replies if x["rpid"] not in s["replied"]]
        master = next((x for x in avail if x["mid"] == MASTER_UID), None)
        target, kind = None, ""
        if master:
            target, kind = master, "主人"
        elif avail:
            target = sorted(avail, key=lambda a: a["like"], reverse=True)[0]
            kind = "路人"
        if not target:
            continue
        v = {"title": post["bvid"]}
        text = gen_comment(reply_prompt(v, target, kind))
        at = [target["mid"]] if kind == "主人" else None
        res = post_comment(post["aid"], text, target["rpid"], target["rpid"], mk, at_uids=at)
        if res.get("code") == 0:
            s["replied"].append(target["rpid"])
            return {"aid": post["aid"], "kind": kind, "uname": target["uname"],
                    "comment": text, "code": 0}
        return {"aid": post["aid"], "kind": kind, "code": res.get("code"),
                "message": res.get("message")}
    return {"skipped": "没有可回复的新评论"}


def do_one():
    s = get_state()
    log(f"state: {json.dumps(s, ensure_ascii=False)}")

    if bj_hour() < NOON_HOUR:
        log(f"未到中午 {NOON_HOUR} 点（当前 {bj_hour()} 点），跳过")
        return {"skipped": "未到中午12点", "hour": bj_hour()}

    mk = ensure_wbi()
    log("wbi keys 就绪")

    # 主评论：每天唯一一条
    if not s["mainPosted"]:
        cands = search_videos(mk)
        fresh = [c for c in cands if not any(p["bvid"] == c["bvid"] for p in s["posted"])]
        if not fresh:
            log("没有新候选视频")
            return {"skipped": "没有新候选"}
        v = sorted(fresh, key=lambda a: a["play"], reverse=True)[0]
        log(f"选中视频：{v['bvid']} {v['title']}")
        main_comment = gen_comment(main_prompt(v))
        res1 = post_comment(v["aid"], main_comment + f" @{AT_NICKNAME}", mk=mk,
                            at_uids=[MASTER_UID])
        log(f"主评论返回 code={res1.get('code')} msg={res1.get('message')}")
        if res1.get("code") != 0:
            save_state(s)
            return {"mainCode": res1.get("code"), "mainMessage": res1.get("message")}
        s["mainPosted"] = True
        s["posted"].append({"bvid": v["bvid"], "aid": v["aid"]})
        log(f"主评论已发：{main_comment}")

    # 回复轮（每天上限 8 条）
    if s["replyCount"] >= REPLY_DAILY_LIMIT:
        out_reply = {"skipped": f"今日回复已达上限{REPLY_DAILY_LIMIT}条"}
    else:
        out_reply = reply_pass(s, mk)
        if out_reply.get("code") == 0:
            s["replyCount"] += 1
        log(f"回复结果：{json.dumps(out_reply, ensure_ascii=False)}")

    save_state(s)
    return {"mainPosted": s["mainPosted"], "reply": out_reply,
            "replyCount": s["replyCount"]}


if __name__ == "__main__":
    try:
        result = do_one()
        log(f"完成：{json.dumps(result, ensure_ascii=False)}")
    except Exception as e:
        log(f"运行失败：{type(e).__name__}: {e}")
        raise
