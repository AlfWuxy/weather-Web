"""本地社区应用入口；默认只允许本机访问，不修改正式天气通服务。"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import errno
from pathlib import Path
import sys
from urllib.parse import urlparse, parse_qs, unquote

from .community_service import CommunityService, estimates, readiness, ROOT
from .community_store import CommunityError, json_text
from .models import InputError
from .engine import PlanInterrupted
from .collection_identity import local_collection_key


class AppServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True

    def __init__(self,address,service,web_root=None):
        self.service=service
        self.web_root=Path(web_root or ROOT/"web").resolve()
        try:
            self.collection_key=local_collection_key(service.store.path)
        except CommunityError:
            # 新导出功能的密钥故障不能阻断原有查看、保存和完整备份。
            self.collection_key=None
        super().__init__(address,AppHandler)


class AppHandler(BaseHTTPRequestHandler):
    server_version="YilaoLocal/0.2"

    def log_message(self,*args):
        # 不把农活、健康资料或完整请求写进访问日志。
        pass

    def _local(self,writing=False):
        port=self.server.server_address[1]
        hosts={f"127.0.0.1:{port}",f"localhost:{port}"}
        if self.headers.get("Host","") not in hosts:
            raise CommunityError("此应用只允许本机同源访问",code="HOST_REJECTED",status=403)
        origin=self.headers.get("Origin")
        if origin and origin not in {"http://"+host for host in hosts}:
            raise CommunityError("不接受来自其它网页的请求",code="ORIGIN_REJECTED",status=403)
        if self.headers.get("Sec-Fetch-Site")=="cross-site":
            raise CommunityError("不接受跨站请求",code="ORIGIN_REJECTED",status=403)
        if writing and self.headers.get("Content-Type","").split(";")[0].strip()!="application/json":
            raise CommunityError("写入资料需要JSON请求",code="CONTENT_TYPE",status=415)

    def _send(self,status,data,kind="application/json; charset=utf-8",extra=None):
        raw=json_text(data).encode() if kind.startswith("application/json") else data
        self.send_response(status)
        self.send_header("Content-Type",kind)
        self.send_header("Content-Length",str(len(raw)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'")
        for k,v in (extra or {}).items(): self.send_header(k,v)
        self.end_headers()
        if self.command!="HEAD": self.wfile.write(raw)

    def _body(self):
        try: length=int(self.headers.get("Content-Length","0"))
        except ValueError: raise CommunityError("请求长度错误") from None
        if not 0<length<=4_000_000: raise CommunityError("请求为空或超过4MB",status=413)
        self.connection.settimeout(20)
        try:
            data=json.loads(self.rfile.read(length),parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError,UnicodeError,RecursionError): raise CommunityError("JSON资料无法读取") from None
        if not isinstance(data,dict): raise CommunityError("请求须为JSON对象")
        return data

    def _dispatch(self):
        writing=self.command in {"POST","PUT"}
        self._local(writing)
        parsed=urlparse(self.path)
        path=parsed.path
        query=parse_qs(parsed.query)
        mode=query.get("mode",["real"])[0]
        service=self.server.service
        if self.command in {"GET","HEAD"}:
            if path=="/api/health": return self._send(200,{"ok":True,"version":"0.2.0","scope":"local-community-pilot","field_validated":False})
            if path=="/api/state": return self._send(200,service.get_state(mode))
            if path=="/api/catalog":
                from .community_catalog import get_community_catalog
                return self._send(200,get_community_catalog())
            if path=="/api/estimates": return self._send(200,estimates(service.get_state(mode)))
            if path=="/api/readiness": return self._send(200,readiness(service.get_state(mode)))
            if path=="/api/rate-review": return self._send(200,service.rate_review({"mode":mode,"task_id":query.get("task_id",[None])[0],"worker_id":query.get("worker_id",[None])[0]}))
            if path=="/api/predictions/review": return self._send(200,service.prediction_review(mode))
            if path=="/api/predictions/preview":
                data={"mode":mode,"task_id":query.get("task_id",[None])[0],"worker_id":query.get("worker_id",[None])[0]}
                if "quantity_value" in query or "quantity_unit" in query:
                    try: value=float(query.get("quantity_value",[""])[0])
                    except ValueError: raise CommunityError("请填写这次预计完成的量",field="quantity") from None
                    data["quantity"]={"value":value,"unit":query.get("quantity_unit",[None])[0]}
                return self._send(200,service.prediction_preview(data))
            if path=="/api/export": return self._send(200,service.export(mode),extra={"Content-Disposition":f'attachment; filename="yilao-{mode}.json"'})
            if path=="/api/field-collection":
                query=parse_qs(parsed.query,keep_blank_values=True)
                if set(query)-{"mode"} or any(len(values)!=1 for values in query.values()):
                    raise CommunityError("查询参数重复或不属于此接口")
                mode=query.get("mode",["real"])[0]
                if mode not in {"real","demonstration"}:
                    raise CommunityError("资料模式无效",code="MODE_CONFLICT")
                if self.server.collection_key is None:
                    raise CommunityError("记录导出的编号密钥暂不可用，请由维护人员检查后重启；原记录及完整备份仍可使用",
                                         code="COLLECTION_KEY_UNAVAILABLE",status=503)
                return self._send(200,service.field_collection(mode,export_secret=self.server.collection_key),
                                  extra={"Content-Disposition":'attachment; filename="yilao-field-collection-draft.json"'})
            if path=="/api/field-template":
                file=ROOT/"examples/field_records_template.csv"
                return self._send(200,file.read_bytes(),"text/csv; charset=utf-8",{"Content-Disposition":'attachment; filename="field_records_template.csv"'})
            if path.startswith("/api/"): raise CommunityError("没有此接口",code="NOT_FOUND",status=404)
            requested="index.html" if path=="/" else unquote(path).lstrip("/")
            file=(self.server.web_root/requested).resolve()
            if not file.is_relative_to(self.server.web_root) or file.suffix not in {".html",".css",".js",".svg",".png",".ico",".json"} or not file.is_file():
                raise CommunityError("文件不存在",code="NOT_FOUND",status=404)
            return self._send(200,file.read_bytes(),mimetypes.guess_type(str(file))[0] or "application/octet-stream")
        data=self._body()
        if self.command=="PUT" and path=="/api/state": return self._send(200,service.save(data))
        if self.command=="POST":
            actions={"/api/demo":lambda:service.demo(),"/api/plan":lambda:service.plan(data),
                     "/api/weather/refresh":lambda:service.refresh_weather(data),"/api/weather/import":lambda:service.import_weather(data),"/api/feedback":lambda:service.feedback(data),
                     "/api/rate-review/adopt":lambda:service.adopt_rate_review(data),
                     "/api/predictions/freeze":lambda:service.freeze_prediction(data),
                     "/api/import":lambda:service.import_bundle(data)}
            if path in actions: return self._send(200,actions[path]())
        raise CommunityError("没有此接口",code="NOT_FOUND",status=404)

    def _handle(self):
        try: self._dispatch()
        except CommunityError as exc:
            self._send(exc.status,{"error":{"code":exc.code,"message":str(exc),"field":exc.field}})
        except InputError as exc:
            self._send(422,{"error":{"code":"PLAN_INPUT_INVALID","message":str(exc),"field":getattr(exc,"path",None)}})
        except PlanInterrupted:
            self._send(422,{"error":{"code":"SEARCH_TIME_LIMIT","message":"本次计算达到时间上限，未保存不完整安排；请缩短规划时段或减少任务后重试"}})
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception as exc:
            print(f"本地应用错误: {type(exc).__name__}",file=sys.stderr)
            self._send(500,{"error":{"code":"INTERNAL_ERROR","message":"本次处理未完成，已保存资料保留；请检查本地运行记录"}})

    do_GET=_handle
    do_HEAD=_handle
    do_POST=_handle
    do_PUT=_handle


def main(argv=None):
    parser=argparse.ArgumentParser(description="宜老农业本地社区工作台")
    parser.add_argument("--port",type=int,default=8765)
    parser.add_argument("--data-dir",default=str(ROOT/"runtime"))
    args=parser.parse_args(argv)
    if args.port!=0 and not 1024<=args.port<=65535: parser.error("端口须为0（自动选择）或1024至65535")
    data=Path(args.data_dir)
    service=CommunityService(data/"community.sqlite3",data/"weather_archive")
    try:
        server=AppServer(("127.0.0.1",args.port),service)
    except OSError as exc:
        if exc.errno!=errno.EADDRINUSE: raise
        server=AppServer(("127.0.0.1",0),service)
        print(f"端口 {args.port} 已被使用，已自动选择空闲端口；其它服务保持原样。",flush=True)
    print(f"宜老农业已启动：http://127.0.0.1:{server.server_address[1]}\n仅本机可访问；资料保存在 {data.resolve()}。按 Ctrl+C 退出。",flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__=="__main__": main()
