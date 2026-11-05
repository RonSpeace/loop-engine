"""skills 蓝图：技能库（技能 v2）——列表 / 详情 / 本机覆盖读写。

读接口任意来源可用；写接口（PUT/POST/DELETE）只接受本机回环来源（看板本就只监听 127.0.0.1，
这里再兜一层，防经反向代理暴露后被远端改写）。项目技能只读（403），全局默认不可删除。
"""


from flask import Blueprint, jsonify, request


from board_modules.services.skill_library import SkillReject
from board_modules.services.skill_library import create_skill
from board_modules.services.skill_library import delete_skill
from board_modules.services.skill_library import get_skill
from board_modules.services.skill_library import list_skills
from board_modules.services.skill_library import put_skill


bp = Blueprint("skills", __name__)


_LOOPBACK_ADDRS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _skills_write_forbidden():
    """写操作来源校验：非回环地址 → 403 响应；放行返回 None。"""
    if (request.remote_addr or "") in _LOOPBACK_ADDRS:
        return None
    return jsonify({"error": "技能库写操作只允许本机（127.0.0.1）访问"}), 403


def _skills_reject(e):
    return jsonify(e.payload), e.status


@bp.route('/api/skills', methods=['GET'])
def api_skills_list():
    """技能列表：每条含作用域、状态、激活方式、角色挂载、校验结果与近 30 天加载次数。"""
    return jsonify(list_skills())


@bp.route('/api/skills', methods=['POST'])
def api_skills_create():
    """新建技能草稿（写 prompts.local/skills/<名>/SKILL.md）。"""
    denied = _skills_write_forbidden()
    if denied:
        return denied
    try:
        return jsonify(create_skill(request.get_json(silent=True))), 201
    except SkillReject as e:
        return _skills_reject(e)


@bp.route('/api/skills/<name>', methods=['GET'])
def api_skill_get(name):
    """技能详情；?project=<名> 时按该项目的作用域链取有效版本（项目技能优先）。"""
    try:
        return jsonify(get_skill(name, request.args.get('project') or None))
    except SkillReject as e:
        return _skills_reject(e)


@bp.route('/api/skills/<name>', methods=['PUT'])
def api_skill_put(name):
    """保存本机覆盖（表单字段或 {"text": SKILL.md 全文}）；矩阵编辑 = 只改 roles / activation。"""
    denied = _skills_write_forbidden()
    if denied:
        return denied
    try:
        return jsonify(put_skill(name, request.get_json(silent=True), request.args.get('project') or None))
    except SkillReject as e:
        return _skills_reject(e)


@bp.route('/api/skills/<name>', methods=['DELETE'])
def api_skill_delete(name):
    """删除本机覆盖 / 草稿（恢复默认）。"""
    denied = _skills_write_forbidden()
    if denied:
        return denied
    try:
        return jsonify(delete_skill(name))
    except SkillReject as e:
        return _skills_reject(e)
