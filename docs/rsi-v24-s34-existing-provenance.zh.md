# S3.4 已有31份DEV来源的溯源与家族账本

结论：**BLOCKED_DATA**。31个原始来源哈希逐项继承并核对S33元数据，来源准入仍为 eligible=0、blocked=0、unknown=31；文档组31不等于独立家族31，已准入独立家族0。公开权威页面补充了DSM相关正式标准和九寨沟历史背景，但没有证明本地文本的完整上游链，也没有签发新家族、Gold或人工复核。

本次读取S33 DEV来源映射、12题草案已有标注、31个明确DEV允许路径的字节（仅计算SHA256）及公开官方网页；未读取FINAL正文、混合Gold或其他题池。重新核对31/31字节哈希匹配、0缺失、0不匹配，共6,163,727 bytes，与S33回执一致；不输出或复制原文。逐项完整字段见 [JSON账本](evidence/s34-existing-provenance.json)。

## DSM与九寨沟的权威证据及边界

DSM指《全球地理信息资源数字表面模型生产技术规范（报批稿）》。[全国标准信息公共服务平台](https://std.samr.gov.cn/hb/search/stdHBDetailedCNF?id=EE20C35DC1AE9E47E05397BE0A0A7656)确认同题名正式标准CH/T 9033-2022，主管部门自然资源部，发布日期2022-07-10、实施日期2022-11-01，查询时标为现行。该元数据只能建立题名相关正式版本候选；没有原报批稿发布URL、日期、版本差异和采集链，不能把冻结报批稿升级为正式标准，也不能从公开可见推导复制/再分发授权。

[九寨沟管理局2020-09-26官方文章](https://www.jiuzhai.com/dynamic/work/7279-2020-09-26-11-01-35)记载震后损伤、后续治理及评审背景。它是可靠的历史背景，不是已证明的本地短文上游；站点版权页脚没有给出本任务所需的明确再利用许可。局部文本的2017年当时风险表述必须保留时间与对象范围，不能由此推出2026年现状。

其余29份没有从现有元数据可靠识别到可签发的公开上游。UUID、哈希、文件名、公司名、issue/PR样式和数据集线索均不足以证明出版社、真实组织、源版本、翻译/转载链、授权或家族独立性；本次没有将这些线索作为事实搜索后强行归属。

## 31项元数据覆盖

| 序号 | 已有文档身份 | raw SHA256（前12位） | 上游核查 | 授权 | 准入 |
| --- | --- | --- | --- | --- | --- |
| 1 | `bee35f97-cba0-43ea-9ef9-ee8efdaf022b` | `a00fca96fed4` | 正式版本题名候选，链未证实 | UNKNOWN | UNKNOWN |
| 2 | `a23a5079-13a1-4481-b316-0a056ac6835c` | `447c4da807f2` | UNKNOWN | UNKNOWN | UNKNOWN |
| 3 | `b5c6a4a3-7445-4b82-a533-6efb13925a61` | `c4bc1404906b` | UNKNOWN | UNKNOWN | UNKNOWN |
| 4 | `ef0cf2bd-8467-48ed-abf7-419a6aeb48e0` | `8f49f93f1665` | UNKNOWN | UNKNOWN | UNKNOWN |
| 5 | `84139d05-3bfa-4032-9bc4-a3c5de980693` | `b83d234b12a7` | UNKNOWN | UNKNOWN | UNKNOWN |
| 6 | `eac25116-7e50-4382-89fd-c12a3c28f403` | `aa6f4afcee38` | UNKNOWN | UNKNOWN | UNKNOWN |
| 7 | `f2f1351f-d86d-4a73-acf8-111552fbd343` | `3d241965e0db` | UNKNOWN | UNKNOWN | UNKNOWN |
| 8 | `7871df83-8bb1-4261-a1a7-83e3954a2e74` | `6810b63a46b2` | UNKNOWN | UNKNOWN | UNKNOWN |
| 9 | `aadeae6a-3884-43e8-9433-85078cd97fef` | `a2825606e762` | UNKNOWN | UNKNOWN | UNKNOWN |
| 10 | `dsid_3c3ab5813b264367877e0d02f652ca32` | `0e1d2fcf70bf` | UNKNOWN | UNKNOWN | UNKNOWN |
| 11 | `dsid_8ca703d991844bd195b4b8fdc16040fd` | `ae8916de431f` | UNKNOWN | UNKNOWN | UNKNOWN |
| 12 | `dsid_38aee49cad12452f8b11574723a578be` | `b5d6164c6150` | UNKNOWN | UNKNOWN | UNKNOWN |
| 13 | `dsid_fb5209e0e305478d8ba56f87f8a8df57` | `1e0a84f2cf6b` | UNKNOWN | UNKNOWN | UNKNOWN |
| 14 | `aeb51a7c-bf57-4d94-ae8a-12074230297d` | `731d9e51e799` | 官方历史背景，链未证实 | UNKNOWN | UNKNOWN |
| 15 | `b0a6ad9c-7164-4a40-b91e-46d9aa225661` | `778590ccc49d` | UNKNOWN | UNKNOWN | UNKNOWN |
| 16 | `dc3716d9-caef-406b-8e5d-e96d53e0934e` | `7d13314dd3e6` | UNKNOWN | UNKNOWN | UNKNOWN |
| 17 | `dsid_9dff54e0ef8e4710926767a81224d475` | `4cab4fc8086f` | UNKNOWN | UNKNOWN | UNKNOWN |
| 18 | `dsid_c18e7b288af34add92570d67b1980bda` | `e64dd3ff07fa` | UNKNOWN | UNKNOWN | UNKNOWN |
| 19 | `dsid_39be65662e0541c9868dfe3d331d5654` | `94971fa402c5` | UNKNOWN | UNKNOWN | UNKNOWN |
| 20 | `dsid_6380c5b842d84b5eaf35b3b6eb7d5778` | `878beb372db4` | UNKNOWN | UNKNOWN | UNKNOWN |
| 21 | `dsid_ab6cc1587c2c491fb4a402f2c6a8342a` | `fe91597f0d61` | UNKNOWN | UNKNOWN | UNKNOWN |
| 22 | `dsid_103ecfafe4fc4e0596414b6b4f98d17e` | `28f11a560980` | UNKNOWN | UNKNOWN | UNKNOWN |
| 23 | `dsid_d283ff2efa944227b3bcad9a4e3d28f4` | `ee5e85b7efdc` | UNKNOWN | UNKNOWN | UNKNOWN |
| 24 | `dsid_d147be61f81249dd85ef4ad13b81d44c` | `3abb00247be1` | UNKNOWN | UNKNOWN | UNKNOWN |
| 25 | `dsid_e5ee3d516eef4049924b02f44abf3ad1` | `650536985a15` | UNKNOWN | UNKNOWN | UNKNOWN |
| 26 | `dsid_09d52038c96e46baac0cceb55d39bd49` | `d18f61754a9b` | UNKNOWN | UNKNOWN | UNKNOWN |
| 27 | `dsid_a559687e57f24826b4beecfcd6322d48` | `46d3ec6a1bd4` | UNKNOWN | UNKNOWN | UNKNOWN |
| 28 | `dsid_ec25fde45afd4900afd2818e6ef4e327` | `66ff071f5c9c` | UNKNOWN | UNKNOWN | UNKNOWN |
| 29 | `dsid_3a1f6951df694270beffed49a6a3cabc` | `75e242042fb3` | UNKNOWN | UNKNOWN | UNKNOWN |
| 30 | `dsid_36f47da3bf1e4303bd75c020a9b2ed4f` | `9aa62436534a` | UNKNOWN | UNKNOWN | UNKNOWN |
| 31 | `dsid_70124860f38d4604981c3c41c45308ce` | `f3f64bec6491` | UNKNOWN | UNKNOWN | UNKNOWN |

每行JSON保留完整原始SHA256、源相对路径、原文档/文件身份、DEV题号、冻结版本、上游版本、暴露与允许用途字段。所有来源明确保留上游授权UNKNOWN；当前DEV审计许可与上游版权授权是不同事实。FINAL使用仍未授权。

## 家族图与暴露

图的31个raw节点全部连接保守UNKNOWN依赖分量。这是未知时不按文件独立计算的记账规则，不声称31份确实同源。DSM→正式标准为题名相关候选边；JZG→官方报道为历史背景边，两条都标记lineage_verified=false。不同标题、UUID、哈希与格式均未产生新的独立家族。

所有来源已在DEV绑定和审计中暴露；DSM/JZG还经历S32草案和S33暂定标注。优化器的完整暴露史仍UNKNOWN，不能恢复为新鲜终测。仅复用S33不透明FINAL精确哈希交集0的元数据，家族层交集仍UNKNOWN；没有读取FINAL内容做推断。

## 12道旧草案状态

| 草案 | 已有native证据 | 当前状态 | 人工签名 |
| --- | --- | --- | --- |
| DRAFT-DSM-01 | 目标成员不在capture窗口，完整池UNKNOWN | DRAFT_UNGRADED_NOT_ADMITTED | 不可用，未签发 |
| DRAFT-DSM-02 | 目标成员不在capture窗口，完整池UNKNOWN | DRAFT_UNGRADED_NOT_ADMITTED | 不可用，未签发 |
| DRAFT-DSM-03 | 目标成员不在capture窗口，完整池UNKNOWN | DRAFT_UNGRADED_NOT_ADMITTED | 不可用，未签发 |
| DRAFT-DSM-04 | 目标成员不在capture窗口，完整池UNKNOWN | DRAFT_UNGRADED_NOT_ADMITTED | 不可用，未签发 |
| DRAFT-DSM-05 | 目标成员不在capture窗口，完整池UNKNOWN | DRAFT_UNGRADED_NOT_ADMITTED | 不可用，未签发 |
| DRAFT-DSM-06 | 目标成员不在capture窗口，完整池UNKNOWN | DRAFT_UNGRADED_NOT_ADMITTED | 不可用，未签发 |
| DRAFT-DSM-07 | 目标成员不在capture窗口，完整池UNKNOWN | DRAFT_UNGRADED_NOT_ADMITTED | 不可用，未签发 |
| DRAFT-DSM-08 | 目标成员不在capture窗口，完整池UNKNOWN | DRAFT_UNGRADED_NOT_ADMITTED | 不可用，未签发 |
| DRAFT-JZG-01 | capture内1个支持成员，完整池UNKNOWN | DRAFT_UNGRADED_NOT_ADMITTED | 不可用，未签发 |
| DRAFT-JZG-02 | capture内1个支持成员，完整池UNKNOWN | DRAFT_UNGRADED_NOT_ADMITTED | 不可用，未签发 |
| DRAFT-JZG-03 | capture内1个支持成员，完整池UNKNOWN | DRAFT_UNGRADED_NOT_ADMITTED | 不可用，未签发 |
| DRAFT-JZG-04 | capture内1个支持成员，完整池UNKNOWN | DRAFT_UNGRADED_NOT_ADMITTED | 不可用，未签发 |

DSM 8题继续为UNKNOWN_TARGET_MEMBER_NOT_IN_CAPTURE_WINDOW；没有把窗口外不存在推成完整原生池无成员。JZG 4题保留已知单支持成员几何阻塞，完整池和实际seed仍UNKNOWN；没有用拆块、复制或删seed补控制。12题独立语义审查、必要atom、真实seed、每atom合法配对控制与Gold签名均未完成。AI暂定标注和网页检索不能代替人类签名。

## 验证、复用与收尾

本次是元数据与公开来源审查，没有模型调用、回放或性能测试。交付校验检查31行/31唯一哈希、12个旧草案、输入SHA256与分母不变；原始哈希与写入命令exit 0；交付解析及断言确认31/31哈希匹配、31唯一源、12旧草案、0新准入，未出现跳过/空行；输出仅计数未截断。公开搜索中未能建立的关系保留UNKNOWN，没有复制私有全文。

复用S33两份已有账本与现有Python/worktree；新增本报告和JSON账本，容器、数据库/索引/语料副本、新暴露事件与删除均为0。保留冻结资料、Gold、日志及UNKNOWN记录以供恢复。E盘起始空闲55,676,055,552 bytes；结束值记录于JSON（共享磁盘可能同时被其他任务改动，差额不等同本任务消耗）。
