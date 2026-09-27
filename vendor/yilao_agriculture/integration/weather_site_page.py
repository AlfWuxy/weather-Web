"""把受版本控制的农业界面放入原站模板，保留独立页面的账户隔离约束。"""
from html.parser import HTMLParser
import secrets


class _AppFragment(HTMLParser):
    """只接受完整、唯一的农业根节点；脚本和头部资源由原站模板统一加载。"""

    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.depth = 0
        self.start = self.end = None
        self.matches = 0

    def source_position(self):
        line, column = self.getpos()
        return sum(map(len, self.lines[:line - 1])) + column

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id") == "agri-app":
            self.matches += 1
            if tag != "div" or self.depth:
                raise ValueError("农业根节点必须是唯一的div")
            self.start = self.source_position()
            self.depth = 1
        elif self.depth and tag == "div":
            self.depth += 1
        if self.depth and tag in {"script", "style", "link", "meta", "iframe", "base"}:
            raise ValueError("农业内容不能自行声明页面资源或嵌入外站")

    def handle_endtag(self, tag):
        if tag == "div" and self.depth:
            self.depth -= 1
            if self.depth == 0:
                self.end = self.source_position() + len("</div>")


def app_fragment(source):
    parser = _AppFragment(source)
    parser.feed(source)
    parser.close()
    if parser.matches != 1 or parser.start is None or parser.end is None or parser.depth:
        raise ValueError("农业页面缺少完整唯一的agri-app根节点")
    fragment = source[parser.start:parser.end]
    # 输入仅来自本地版本文件，不能传入请求正文或用户提供的HTML。
    return fragment.replace('<div id="agri-app"', '<div id="agri-app" data-host="weather-site"', 1)


def render_weather_site_page(context, source, *, template):
    from flask import Response, render_template
    from markupsafe import Markup

    nonce = secrets.token_urlsafe(24)
    html = render_template(template, agriculture=context, agriculture_nonce=nonce,
                           agriculture_body=Markup(app_fragment(source)))
    response = Response(html, content_type="text/html; charset=utf-8")
    # 只允许原站已使用的图标CDN；内联脚本必须携带本次响应的随机nonce。
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src-attr 'unsafe-inline'; font-src 'self' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'")
    return response
