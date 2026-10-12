"""补齐缺失的 AppModelConfig 行（容器内运行，用 Dify 自己的模型类）。

为什么要补
----------
apps.app_model_config_id 是 NULL，且 app_model_configs 表整表为空。
Dify 的 message_cycle_manager.handle_retriever_resources 第一行就是：

    if not app_config.additional_features:
        raise ValueError("Additional features not found")

additional_features 是从 AppModelConfig 行算出来的，行不存在 -> 为 None ->
知识检索节点一旦抛出 RunRetrieverResourceEvent 就会在这里被打死，
于是 API 返回里 retriever_resources 恒为 []，FR-06 引用组装拿不到任何数据。

注意 to_dict() 走的是带默认值的 property getter（如
retriever_resource_dict -> _get_enabled_config(..., default_enabled=True)），
所以列留 NULL 不会炸，真正缺的是行本身 + 外键。

用法：
    docker cp fix_app_model_config.py docker-api-1:/tmp/
    docker exec -e PYTHONIOENCODING=utf-8 docker-api-1 python /tmp/fix_app_model_config.py [--apply]
"""

import json
import sys

sys.path.insert(0, "/app/api")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app_factory import create_app  # noqa: E402
from extensions.ext_database import db  # noqa: E402
from models.model import App, AppModelConfig  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

APP_ID = "f5939e3a-5fd7-43e7-8824-54d6ed1bc8da"

# 显式打开引用返回；query 变量指向 sys.query，让检索节点拿到用户问题
MODEL_CONFIG_DICT = {
    "opening_statement": None,
    "suggested_questions": [],
    "suggested_questions_after_answer": {"enabled": False},
    "speech_to_text": {"enabled": False},
    "text_to_speech": {"enabled": False, "voice": "alloy", "language": "en-US", "autoPlay": "disabled"},
    "retriever_resource": {"enabled": True},
    "annotation_reply": {"enabled": True},
    "more_like_this": {"enabled": False},
    "sensitive_word_avoidance": {"enabled": False, "type": "AND", "configs": [], "rule_id": ""},
    "external_data_tools": [],
    "model": {
        "provider": "langgenius/ollama/ollama",
        "name": "qwen3.5:9b",
        "mode": "chat",
        "completion_params": {"temperature": 0.1},
    },
    "user_input_form": [],
    "dataset_query_variable": "sys.query",
    "pre_prompt": None,
    "agent_mode": {"enabled": False},
    "prompt_type": "simple",
    "chat_prompt_config": {"prompts": []},
    "completion_prompt_config": {"prompt": {}},
    "dataset_configs": {"datasets": [], "retriever_model": None},
    "file_upload": {
        "image": {
            "enabled": False,
            "number_limits": 3,
            "transfer_methods": ["local_file", "remote_url"],
        }
    },
}


def main() -> int:
    apply = "--apply" in sys.argv
    # db.engine 是 flask_sqlalchemy 的，必须挂在 app context 里。
    # Dify 的 create_app() 返回 (socketio.WSGIApp, DifyApp)，
    # Flask app 是第 2 个，取错会得到没有 app_context 的 socketio 包装。
    created = create_app()
    flask_app = created[1] if isinstance(created, tuple) else created

    with flask_app.app_context():
        return _run(apply)


def _run(apply: bool) -> int:
    with Session(db.engine) as session:
        app = session.get(App, APP_ID)
        if app is None:
            print("[x] 找不到 app", APP_ID)
            return 2

        print(f"[*] app = {app.name}  mode={app.mode}")
        print(f"[*] 当前 app_model_config_id = {app.app_model_config_id}")

        existing = session.query(AppModelConfig).filter(AppModelConfig.app_id == APP_ID).first()
        if existing:
            print(f"[*] 已存在 AppModelConfig 行 id={existing.id}，改为更新其字段")
            cfg = existing
        else:
            print("[*] 不存在 AppModelConfig 行，将新建")
            cfg = AppModelConfig(app_id=APP_ID)

        cfg.from_model_config_dict(MODEL_CONFIG_DICT)

        d = cfg.to_dict()
        print(f"[*] to_dict().retriever_resource   = {d['retriever_resource']}")
        print(f"[*] to_dict().dataset_query_variable = {d['dataset_query_variable']}")
        print(f"[*] to_dict().model               = {d['model']}")

        if not apply:
            print("\n[*] 预览模式。加 --apply 写库。")
            return 0

        session.add(cfg)
        session.flush()
        app.app_model_config_id = cfg.id
        session.commit()

        print(f"\n[OK] AppModelConfig.id = {cfg.id}")
        print(f"[OK] apps.app_model_config_id 已指向它")

    # 复核：重新读一次，确认落库后 to_dict 仍带 enabled
    with Session(db.engine) as session:
        app2 = session.get(App, APP_ID)
        cfg2 = session.get(AppModelConfig, app2.app_model_config_id)
        print(f"\n[复核] app_model_config_id = {app2.app_model_config_id}")
        print(f"[复核] retriever_resource = {cfg2.to_dict()['retriever_resource']}")
        print(f"[复核] dataset_query_variable = {cfg2.to_dict()['dataset_query_variable']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())