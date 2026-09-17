# DeepSeek 批量翻译 HTML 工具

用 DeepSeek API 批量翻译 HTML 文件的桌面工具（tkinter 界面）。
自动提取 HTML 里的文本节点和属性（alt / title / placeholder / aria-label），
批量送翻译，再原样回写，保留原有标签结构。

## 功能

- 批量翻译 `input/` 目录下的所有 HTML 文件，输出到 `output/`
- 可视化界面：选文件夹、填语言、点按钮就开始
- **两个独立输入框**：自己填源语言和目标语言（默认 俄语 → 简体中文）
- **进度条**实时显示，日志实时滚动
- **自动缓存**：翻过的内容第二次直接命中缓存，省 API 费用
- **批内去重**：同一批里重复的句子只发一次 API
- **跳过未改动文件**：源文件没变就不重复翻译
- 支持**单独翻译某一个文件**（左栏双击文件）
- 支持**中途停止**

## 安装

1. 装好 Python 3.8+（建议 3.10 以上）
2. 安装依赖：

pip install -r requirements.txt

## 使用

1. 在项目文件夹里新建 `input` 文件夹，把要翻译的 HTML 放进去
2. 运行 `GUI文件.py`
3. 填入你的 DeepSeek API Key（去 https://platform.deepseek.com 申请）
4. 选择源语言和目标语言，点「全部翻译」
5. 译文会输出到 `output` 文件夹

> 注意：`GUI文件.py` 和 `translate_html.py` 必须在同一文件夹内。
