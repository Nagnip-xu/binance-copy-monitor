# 项目官网

中文静态网站，无构建步骤、无前端框架、无追踪脚本。此目录仅介绍桌面程序，不提供网页跟单或账户登录。

## 本地预览

直接用浏览器打开本目录 `index.html`，或者从项目根目录运行：

```powershell
python -m http.server 8765 --bind 127.0.0.1 --directory site
```

访问 `http://127.0.0.1:8765/`。请务必使用 `--directory site`，不要将含本地账户资料的整个项目作为网站根目录。

## 页面与资源

- `index.html`：中文首页、真实界面、使用步骤、常见问题和下载。
- `styles.css`：响应式布局、状态与减少动态支持。
- `app.js`：三种模式的本地模拟演示、导航、截图放大和校验值复制。
- `assets/`：品牌几何图标、已有脱敏截图、自托管字体与许可。

演示不会连接账户或发起交易。下载链接指向已经发布的 v0.2.0 Windows 版本。

线上地址：[https://nagnip-xu.github.io/binance-copy-monitor/](https://nagnip-xu.github.io/binance-copy-monitor/)。网站由仓库根目录的 GitHub Actions 工作流发布，工作流只上传 `site/`，不会把项目根目录的本地账户数据作为网页内容。

## 字体与图片

页面自托管 [Noto Sans SC](https://github.com/google/fonts/tree/main/ofl/notosanssc) 与 [Barlow](https://github.com/google/fonts/tree/main/ofl/barlow) 的 WOFF 子集，许可文本位于 `assets/fonts/`。中文子集覆盖当前页面和交互文案；新增中文时，应补充字形子集，避免回退字体造成字重差异。

`assets/main-window.webp` 来自项目已公开的脱敏界面图，为无损转换。它展示真实桌面程序，页面中的海军蓝状态台则是明确标注的网页模拟演示。
