⚠️「{review_name}」评审未通过，已打回本节点复修{round_tag}。
返工输入：未关闭问题（来自评审 result.json 的 issues；逐条修复，并在你的 result.json 加 "issue_responses": [{"id": "问题编号", "resolution": "如何处理 / 不改的理由"}]）：
<card-data source="review.issues">
{issues}
</card-data>
评审产物（完整意见，按需读取）：{artifact}