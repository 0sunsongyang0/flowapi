---
name: flow2api-new-skill
description: >-
  通过用户已配置的本机 Flow2API 为 Codex 和 Antigravity 生成或修改图片，保存真实 PNG 并返回预览。
  用户要求生图、画图、图生图、生成插画、图标、横幅或项目配图时使用；支持本地参考图和多账号独立任务批量生成。
  Use for image generation and reference-image editing through the configured flow2api MCP server.
---

# Flow2API 多账号生图

使用已安装的 `flow2api` MCP 服务生成图片。服务会读取本机私密配置，无需用户再次提供 API Key 或 Google Cookie。用户明确指定其他图片服务时，遵循其选择。

## 执行流程

1. 从用户请求和当前项目确定图片用途、主体、构图、风格、比例和保存位置。信息足够就直接执行；未指定张数时默认生成一张。保留用户要求的文字、颜色、人物和参考图，不添加与其意图冲突的约束。
2. 选择图片模型。默认使用 Nano Banana 2.1 横图，常用模型如下；要求其他比例、2K/4K 或其他型号时，调用 `flow2api_list_image_models` 获取当前支持的精确 ID，不自行拼接型号。

   | 用途 | 模型 ID |
   | --- | --- |
   | 横图 | `gemini-3.1-flash-image-landscape` |
   | 竖图 | `gemini-3.1-flash-image-portrait` |
   | 方图 | `gemini-3.1-flash-image-square` |

3. 一张图调用 `flow2api_generate_image`；多张独立图片使用 `flow2api_generate_images` 或 CLI `--generate-batch`，每张图一个 job。只有任务明确依赖前一张结果时才顺序执行。已有项目运行器也要检查实际调用链：若仍在 `for` 循环中阻塞等待单图 `--generate`，先将调用端接入批量入口；修改 skill 或后台并发上限不会自动改写旧脚本。各 job 可以是不同主题，也可以是用户要求的同主题变体；不要把同一个 job 复制给多个账号。将输出保存为当前项目中新的、互不重复的绝对 `.png` 路径；沿用项目已有素材目录，未指定时可用 `assets/images/`。没有当前项目时可以省略路径，使用服务默认输出目录。已有文件使用新文件名，随后按用户要求替换引用。
4. 逐张检查工具返回的状态、实际文件路径、宽高和图片预览。批次可能部分完成，不能只看顶层 `success`。确认文件存在并使用本机图片查看工具检查内容后，再报告成功。在回复中优先用 `![图片说明](/absolute/path/result.png)` 展示实际结果；路径必须是实际生成文件的绝对路径。模型名称中的 2K/4K 不等于最终尺寸，应以返回值为准。用户要求接入页面或应用时，继续完成素材引用和必要的页面检查。

## 工具参数

`flow2api_generate_image` 接受：

- `prompt`：非空的完整图片要求，可使用中文。
- `model`：可省略，默认 `gemini-3.1-flash-image-landscape`。
- `output_path`：新的绝对 `.png` 文件路径。
- `reference_image_paths`：可省略，最多四张现有本地 PNG、JPEG 或 WebP 的绝对路径，每张不超过 25 MiB。将需要保留和需要修改的内容写进 prompt；只有路径而未传入此参数，不会使用参考图。
- `account_id`：可省略；指定时只使用这个账号，不能失败后换号重发生同一任务。需要指定账号时先用 `flow2api_list_image_accounts` 获取当前 ID。

例如，当前项目为 `/project` 时，方图请求可以使用：

```json
{
  "prompt": "一只橘猫坐在白色方块上，青绿色背景，柔和光线，简洁插画，主体居中",
  "model": "gemini-3.1-flash-image-square",
  "output_path": "/project/assets/images/orange-cat.png"
}
```

单图调用生成一张图片，批量调用按 jobs 数量生成，都会使用用户的 Google Flow 额度。按用户请求的张数执行；不要为了检查配置额外生成图片。

## 多账号批量任务

`flow2api_generate_images` 接受 `jobs` 数组（1–32 项），每项包含单图的 `prompt`、可选 `model`、`output_path`、`reference_image_paths`；批量工具会自动选择并固定每项使用的账号。可选 `concurrency` 只用于降低本批并发，不会突破账号容量。

```json
{
  "jobs": [
    {
      "prompt": "项目首页横幅：青绿色背景上的橘猫，柔和光线，左侧保留标题空间",
      "output_path": "/project/assets/images/cat-banner.png"
    },
    {
      "prompt": "项目功能图标：一只蓝色小鸟，白色背景，简洁方形插画",
      "model": "gemini-3.1-flash-image-square",
      "output_path": "/project/assets/images/bird-icon.png"
    }
  ]
}
```

- 每账号最多同时执行 8 个独立图片任务，较小的账号配置仍有效；后台也会限制跨批次、跨单图调用的总并发。工具按各模型的可用账号分配任务，超出容量的任务排队。用 `flow2api_list_image_accounts(model=...)` 查看当前模型的账号及 `concurrency_limit`，按返回值计算容量，不把账号数写死。目前配置为两个账号、每号 8 个，合计最多 16 个。
- 提交前会校验全部任务的模型、输出路径和参考图；任一预检失败时整批不提交。输出文件不能已存在，也不能在同批中重复。
- 接续正在运行的旧脚本时，先核对已完成、在跑和未启动任务；确保旧启动器不会再提交相同输出后，才把剩余任务交给新批次。已有成功图片保留，在跑任务等待确认，不通过再启动一份脚本来叠加。
- 包装脚本也必须遵循未知结果不重投；不要无条件按退出码重试，或在重试中静默丢弃用户参考图、删除已生成图片。拆成多个批次块时，响应 `pause_reason.code=account_rejected` 或任一任务 `account_rejected=true` 都要停止后续块，即使当前块的任务已全部启动、没有 `uncompleted`，也不能继续灌入下一块。
- 返回的 `jobs` 按 `job_index` 对应输入，包含状态、`account_id`、保存路径或错误；`manifest_path` 指向持久批次记录。分别展示成功图片，并检查 `partial`、`batch_state` 和 `pause_reason`。未知结果或明确账号身份/模型可用性拒绝会暂停尚未启动的任务，已在运行的任务仍各自返回结果；不会重投或自动换账号。身份/可用性拒绝的任务带 `account_rejected=true`，批次返回 `pause_reason.code=account_rejected`，尚未启动的任务保留 `uncompleted` 和 `submission_attempted=false`。取消后从首张输出目录的 `.flow2api-batch-*.json` 恢复状态，核对对应批次和后台日志。
- 若原图成功但放大结果未知，会返回原图及 `generated_assets`、`warnings` 和 `outcome_unknown`。明确说明未确认放大完成；保存原图，不自动补跑放大或重做整张图。
- 暂停后保留成功图片、已有图片 URL 和全部未知结果，先查后台请求日志。账号拒绝时先修复并核验登录身份及模型可用性，再显式选取确定未提交的任务组成新批次；通常为 `uncompleted` 且 `submission_attempted=false`，并需与请求日志一致。`failed` 本身不能证明未提交，不能盲目重发整批、补跑未知任务或自动继续后续块。
- 8 是本地调度上限；配置和离线并发测试不能证明 Google 会稳定接受每号 8 并发。根据实际明确拒绝、验证码或额度情况降低 `concurrency`。
- 实际吞吐还受浏览器槽位、验证码准备和本机资源影响。排查“仍然逐张”时，同时核对项目是否调用批量入口、批次记录中的在途任务和后台浏览器槽位；终端按顺序打印结果不能单独证明串行。浏览器准备阶段可以限流，取得验证码后应释放准备名额，完整生成任务仍由每账号容量限制。

## 使用上传的参考图

用户在当前客户端对话中上传或拖入图片，并要求根据图片生成时，使用附件对应的真实本地文件。若附件仅提供图像内容，可通过当前环境支持的附件读取或保存功能保存到本地；若无法取得可读文件，再请用户将图片放到项目中并提供路径。不要把对图片的文字描述当作已传入参考图。

查看图片，按用户要求在 prompt 中说明要保留的主体、人物、构图或风格，以及要修改的部分。多张图按 `reference_image_paths` 的顺序对应说明，例如“图1保留主体，图2参考配色”。将全部选定参考图的绝对路径实际传入工具；不要只把路径写在 prompt 中。支持 1–4 张 PNG、JPEG 或 WebP，每张不超过 25 MiB。修改现有图片时将原图作为参考，另存新文件。

例如，用户要求“保留这张图的主体和构图，把背景改成浅蓝色水彩风格”，工具参数可为：

```json
{
  "prompt": "以参考图1为基础，保留主体外观、姿态与构图，将背景改为浅蓝色水彩风格",
  "reference_image_paths": ["/project/references/original.png"],
  "output_path": "/project/assets/images/watercolor-edit.png"
}
```

参考图会随本次生成请求提交到 Google Flow。生成后查看真实结果，核对用户要求保留和修改的内容；不要承诺像素级一致。



## 连接与失败处理

- 检查连接用 `flow2api_check_connection`，检查模型用 `flow2api_list_image_models`，检查模型对应账号及容量用 `flow2api_list_image_accounts`，这些调用不生成图片。账号列表表示配置符合条件，连接成功只证明本机接口可用，不能证明 Google 登录态或生成一定成功。
- 超时、验证码错误或服务返回失败时，先查管理后台的请求日志，确认上次任务结果再决定下一步。不要自动重发生图请求，尤其不要在结果未知时重复消耗额度。恢复旧批次时，先修复并核验故障原因，再仅显式恢复日志确认未提交的任务。
- `Flow 登录认证失败`、上游 401 或 `account_rejected`：停止尚未启动的任务及后续块。Cookie 失效时在后台账号「详情」重新同步并保存有效 Cookie，然后核验实际登录身份；“当前不可用于该模型，或登录验证失败”也可能是模型资格或账号配置问题，需核对原因。不要通过降低并发、换号重投或仅启用账号绕过拒绝，也不要要求用户在聊天中发送凭据或修改本地过期时间绕过验证。
- 没有可用账号：检查后台账号状态和最近错误；不能把活跃账号数当成上游认证已通过的证据。
- 没有返回图片或本地文件未保存成功：明确报告未完成，不把提示词、占位图或以前的图片当成本次结果。

管理后台：`http://127.0.0.1:38000/manage`；API 地址：`http://127.0.0.1:38000/v1`。

## 当前会话看不到 MCP 工具时

先检查当前客户端是否提供 `flow2api` 的 MCP 工具；工具名称可能带有服务命名空间。若本轮尚未发现新工具，直接使用下方命令行入口完成任务。Codex 新安装的 skill 在下一轮可用；工具列表尚未刷新时可重新打开会话。Antigravity 可使用其 MCP 服务器刷新功能，并在新会话中发现新版 skill。按当前客户端的实际入口操作。

文字生图和参考图生图也可使用同一桥接程序的命令行入口，替换提示词和绝对保存路径后执行；它读取现有私密配置，不需要把凭据放进命令：

```bash
'/path/to/flow2api-mcp/.venv/bin/python' \
  '/path/to/flow2api-mcp/server.py' \
  --generate '用户请求的完整图片提示词' --output '/absolute/project/assets/new-image.png'
```

参考图任务在上述命令末尾添加 `--reference '/absolute/path/reference1.png' --reference '/absolute/path/reference2.jpg'`，按需传入 1–4 张图片的真实绝对路径。

多图时把上面批量示例的 `jobs` 数组本身保存为 JSON 文件，改用 `--generate-batch '/absolute/project/jobs.json'`，可加 `--concurrency 4` 收紧本批并发。单图可加 `--account-id 2` 固定账号，先发现当前 ID，不依赖示例数字。

同一命令去掉生图参数，添加 `--check` 可免费检查本机连接；添加 `--list-accounts` 可免费查看账号及容量，支持 `--model MODEL` 筛选。命令输出 JSON 中的逐项实际路径和尺寸，仍需检查保存的图片。
