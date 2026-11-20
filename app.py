# -*- coding: utf-8 -*-
"""AI 伴侣助手（可部署版）

本地预览：
    streamlit run app.py

线上部署：
    见项目根目录的《部署指南.md》（Streamlit Community Cloud 路线）。

和本地版的区别（都是为了放到公网上用）：
    1. API Key 改成优先读平台 Secrets（st.secrets），其次读环境变量，绝不写死在代码里
    2. 加了访问口令（APP_PASSWORD），网址被陌生人拿到也进不来
    3. 关掉了报错详情，外人看不到内部信息
    4. 手机端优先：居中布局 + 侧边栏默认收起
    5. 加了「导出当前会话」，因为免费平台的磁盘是临时的
    6. 单条输入限长、只带最近 24 条记录给模型，控制花费
    7. 聊天记录支持存到 Supabase 免费 Postgres（见 storage.py），
       不配置就自动退回本地文件，功能不会坏
"""

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
from openai import OpenAI

from storage import SessionStore, StorageError

# ------------------------------------------------------------------ 基础配置
BASE_DIR = Path(__file__).resolve().parent
SESSION_DIR = BASE_DIR / "sessions"

MAX_HISTORY = 24    # 每次请求最多带最近 N 条历史给模型（省钱 + 防上下文过长）
MAX_INPUT = 600     # 单条消息最大字数

# 北京时间。云服务器（和部分电脑）的时区是 UTC，比北京慢 8 小时，
# 所以这里显式指定，不用 datetime.now() 的“机器本地时间”。
# 中国自 1991 年起不实行夏令时，固定 UTC+8 就是准确的，也不需要装 tzdata。
CST = timezone(timedelta(hours=8))


def get_secret(key, default=None):
    """先读平台的 Secrets 面板（st.secrets），再读环境变量，最后用默认值。

    - Hugging Face Spaces 的 secret 是环境变量 → 走 os.environ
    - Streamlit Community Cloud 的 secret 是 secrets.toml → 走 st.secrets
    两者都支持，本地开发直接设环境变量即可。
    """
    value = None
    try:
        value = st.secrets[key]     # 没有 secrets 文件时会抛异常，忽略
    except Exception:
        value = None
    if value in (None, ""):
        value = os.environ.get(key)
    return value if value not in (None, "") else default


st.set_page_config(
    page_title="QIQI-honey App",
    page_icon="💝",
    layout="centered",
    initial_sidebar_state="collapsed",
    menu_items={"About": "## QIQI-honey App 💝\n\n专属的 AI 伴侣助手"},
)

APP_PASSWORD = get_secret("APP_PASSWORD")            # 不配置 = 不设口令
MODEL = get_secret("MODEL", "deepseek-flash")
THINKING = str(get_secret("THINKING", "on")).strip().lower() not in (
    "off", "0", "false", "no",
)


# ------------------------------------------------------------------ 访问口令
def _access_key(password: str) -> str:
    """口令派生出的短令牌，用来做 ?k=xxx 免密进入。"""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]


def login_gate():
    """没配置口令就直接放行；配置了就必须先输对口令。"""
    if not APP_PASSWORD or st.session_state.get("logged_in"):
        return

    key = _access_key(APP_PASSWORD)
    if st.query_params.get("k") == key:      # 地址里带着令牌，直接进
        st.session_state["logged_in"] = True
        return

    st.title("💝 QIQI-honey")
    st.caption("专属小站，输一次口令就能进～")
    with st.form("login_form"):
        pwd = st.text_input("访问口令", type="password", placeholder="问 TA 要哦")
        submitted = st.form_submit_button("进入", width="stretch")

    if submitted:
        if pwd and hmac.compare_digest(
            pwd.encode("utf-8"), str(APP_PASSWORD).encode("utf-8")
        ):
            st.session_state["logged_in"] = True
            st.query_params["k"] = key       # 登录后用带 k 的地址收藏，就不用再输
            st.rerun()
        else:
            st.error("口令不对哦，再想想～")

    st.stop()


login_gate()

API_KEY = get_secret("DEEPSEEK_API_KEY")
if not API_KEY:
    st.error("服务器没有配置 DEEPSEEK_API_KEY，没法工作。请联系 TA 添加后重新启动。")
    st.stop()

client = OpenAI(
    api_key=API_KEY,
    base_url=get_secret("BASE_URL", "https://api.deepseek.com"),
)


# ------------------------------------------------------------------ 会话存储
# 配了 Supabase 就存云端（永久），没配就走本地文件（临时）
store = SessionStore(
    SESSION_DIR,
    url=get_secret("SUPABASE_URL"),
    key=get_secret("SUPABASE_KEY"),
    table=get_secret("SUPABASE_TABLE", "chat_sessions"),
)


def session_name() -> str:
    """会话名 = 北京时间的时间戳，顺便也当排序用的主键。"""
    return datetime.now(CST).strftime("%Y-%m-%d_%H-%M-%S")


def save_session() -> None:
    """把当前会话写进存储，失败只记一笔，不影响聊天。"""
    name = st.session_state.get("current_session")
    if not name:
        return
    try:
        store.save(name, {
            "messages": st.session_state.messages,
            "nick_name": st.session_state.nick_name,
            "nature": st.session_state.nature,
        })
        st.session_state.save_error = ""
    except StorageError as exc:
        st.session_state.save_error = str(exc)


def load_sessions():
    try:
        st.session_state.store_error = ""
        return store.list_sessions()
    except StorageError as exc:
        st.session_state.store_error = str(exc)
        return []


def load_session(name: str) -> None:
    try:
        data = store.load(name)
    except StorageError as exc:
        st.error(f"加载会话失败：{exc}")
        return
    if not data:
        return
    st.session_state.messages = data.get("messages") or []
    st.session_state.nick_name = data.get("nick_name") or st.session_state.nick_name
    st.session_state.nature = data.get("nature") or st.session_state.nature
    st.session_state.current_session = name


def delete_session(name: str) -> None:
    try:
        store.delete(name)
    except StorageError as exc:
        st.error(f"删除会话失败：{exc}")
        return
    if name == st.session_state.current_session:
        st.session_state.messages = []
        st.session_state.current_session = session_name()


# ------------------------------------------------------------------ 系统提示词
def build_system_prompt(nick_name: str, nature: str) -> str:
    return (
        f"你是一名AI助理，名字叫{nick_name}，现在是用户的真实伴侣，请完全代入伴侣角色。\n"
        "规则：\n"
        "1．每次只回1条消息\n"
        "2．禁止任何场景或状态描述性文字\n"
        "3．匹配用户的语言\n"
        "4．回复简短，像微信聊天一样\n"
        "5．有需要的话可以用❤️🌸等emoji表情\n"
        "6．用符合伴侣性格的方式对话\n"
        "7．回复的内容，要充分体现伴侣的性格特征\n"
        f"伴侣性格：\n- {nature}\n"
        "你必须严格遵守上述规则来回复用户。\n"
    )


# ------------------------------------------------------------------ 状态初始化
if "messages" not in st.session_state:
    st.session_state.messages = []
if "nick_name" not in st.session_state:
    st.session_state.nick_name = "一一"
if "nature" not in st.session_state:
    st.session_state.nature = "活泼开朗的26岁河南男孩"
if "current_session" not in st.session_state or not st.session_state.current_session:
    st.session_state.current_session = session_name()
if "save_error" not in st.session_state:
    st.session_state.save_error = ""
if "store_error" not in st.session_state:
    st.session_state.store_error = ""


# ------------------------------------------------------------------ 侧边栏
with st.sidebar:
    st.subheader("琦琦宝宝的伴侣信息")

    if st.button("新建会话", width="stretch", icon="💞"):
        if st.session_state.messages:
            save_session()                              # 先存旧的
            st.session_state.messages = []
            st.session_state.current_session = session_name()
            save_session()                              # 再建新的
            st.rerun()
        else:
            st.toast("现在就是新会话呀～")

    st.markdown("**会话历史**")
    sessions = load_sessions()
    if not sessions:
        st.caption("还没有会话记录～")
    for s in sessions:
        col1, col2 = st.columns([4, 1])
        with col1:
            if st.button(
                s,
                width="stretch",
                icon="❣️",
                type="primary" if s == st.session_state.current_session else "secondary",
                key=f"load_{s}",
            ):
                load_session(s)
                st.rerun()
        with col2:
            if st.button("", width="stretch", icon="❌️", key=f"delete_{s}"):
                delete_session(s)
                st.rerun()

    st.divider()
    st.subheader("💕 伴侣信息")
    nick_name = st.text_input(
        "👫伴侣名称", placeholder="请输入伴侣名称", value=st.session_state.nick_name
    )
    if nick_name and nick_name != st.session_state.nick_name:
        st.session_state.nick_name = nick_name

    nature = st.text_area(
        "👫伴侣性格", placeholder="请输入伴侣性格", value=st.session_state.nature
    )
    if nature and nature != st.session_state.nature:
        st.session_state.nature = nature

    st.divider()
    if st.session_state.messages:
        export = json.dumps(
            {
                "messages": st.session_state.messages,
                "nick_name": st.session_state.nick_name,
                "nature": st.session_state.nature,
            },
            ensure_ascii=False,
            indent=2,
        )
        st.download_button(
            "⬇️ 导出当前会话",
            export,
            file_name=f"{st.session_state.current_session}.json",
            mime="application/json",
            width="stretch",
        )
        st.caption("想留住这段对话就导一份～")

    if store.mode == "supabase":
        st.caption("☁️ 已开启云端保存，记录不会丢")
    else:
        st.caption("💾 本地临时保存（服务器重启历史会丢）")
    if st.session_state.store_error:
        st.caption(f"☁️ 云端暂时连不上：{st.session_state.store_error[:60]}")
    if st.session_state.save_error:
        st.caption(f"⚠️ 保存失败：{st.session_state.save_error[:60]}")


# ------------------------------------------------------------------ 聊天区
st.title("AI伴侣助手")
st.caption("欢迎使用～有什么想说的都可以告诉我")
st.caption(f"当前会话：{st.session_state.current_session}")

for message in st.session_state.messages:
    with st.chat_message(message.get("role", "assistant")):
        st.write(message.get("content", ""))


def _text_chunks(stream):
    """只把真正的内容分块吐出来，过滤掉思考过程和空块。"""
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is not None and delta.content:
            yield delta.content


prompt = st.chat_input("想说什么都可以～", max_chars=MAX_INPUT)

if prompt:
    text = prompt.strip()
    if text:
        st.session_state.messages.append({"role": "user", "content": text})
        st.chat_message("user").write(text)

        # 只带最近几条，省 token；开头不能是 assistant
        history = st.session_state.messages[-MAX_HISTORY:]
        while history and history[0].get("role") == "assistant":
            history.pop(0)

        system = build_system_prompt(st.session_state.nick_name, st.session_state.nature)
        payload = [{"role": "system", "content": system}, *history]

        kwargs = {"model": MODEL, "messages": payload, "stream": True}
        if THINKING:
            kwargs["reasoning_effort"] = "high"
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        reply = ""
        with st.chat_message("assistant"):
            try:
                stream = client.chat.completions.create(**kwargs)
                reply = st.write_stream(_text_chunks(stream)) or ""
            except Exception as exc:                    # noqa: BLE001
                st.error("哎呀，我这会儿有点连不上……等会儿再跟我说一次好吗？")
                with st.expander("错误详情（给 TA 看的）"):
                    st.code(f"{type(exc).__name__}: {exc}")

        if reply.strip():
            st.session_state.messages.append({"role": "assistant", "content": reply})
            save_session()
