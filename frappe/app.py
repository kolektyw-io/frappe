# -*- coding: utf-8 -*-
# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

import os
import logging

from werkzeug.local import LocalManager
from werkzeug.wrappers import Request, Response
from werkzeug.exceptions import HTTPException, NotFound
from werkzeug.middleware.profiler import ProfilerMiddleware
from werkzeug.middleware.shared_data import SharedDataMiddleware

import frappe
import frappe.handler
import frappe.auth
import frappe.api
import frappe.utils.response
from frappe.utils import get_site_name, sanitize_html
from frappe.middlewares import StaticDataMiddleware
from frappe.website.serve import get_response
from frappe.utils.error import make_error_snapshot
from frappe.core.doctype.comment.comment import update_comments_in_parent_after_request
from frappe import _
import frappe.recorder
import frappe.monitor
import frappe.rate_limiter

local_manager = LocalManager([frappe.local])

_site = None
_sites_path = os.environ.get("SITES_PATH", ".")

class RequestContext(object):

	def __init__(self, environ):
		self.request = Request(environ)

	def __enter__(self):
		init_request(self.request)

	def __exit__(self, type, value, traceback):
		frappe.destroy()

@Request.application
def application(request):
	response = None

	try:
		rollback = True

		init_request(request)

		frappe.recorder.record()
		frappe.monitor.start()
		frappe.rate_limiter.apply()
		frappe.api.validate_auth()

		if request.method == "OPTIONS":
			response = Response()

		elif frappe.form_dict.cmd:
			response = frappe.handler.handle()

		elif request.path.startswith("/api/"):
			response = frappe.api.handle()

		elif request.path.startswith('/backups'):
			response = frappe.utils.response.download_backup(request.path)

		elif request.path.startswith('/private/files/'):
			response = frappe.utils.response.download_private_file(request.path)

		elif request.method in ('GET', 'HEAD', 'POST'):
			response = get_response()

		else:
			raise NotFound

	except HTTPException as e:
		return e

	except frappe.SessionStopped as e:
		response = frappe.utils.response.handle_session_stopped()

	except Exception as e:
		response = handle_exception(e)

	else:
		rollback = after_request(rollback)

	finally:
		if request.method in ("POST", "PUT") and frappe.db and rollback:
			frappe.db.rollback()

		frappe.rate_limiter.update()
		frappe.monitor.stop(response)
		frappe.recorder.dump()

		log_request(request, response)
		process_response(response)
		frappe.destroy()

	return response

def init_request(request):
	frappe.local.request = request
	frappe.local.is_ajax = frappe.get_request_header("X-Requested-With")=="XMLHttpRequest"

	site = _site or request.headers.get('X-Frappe-Site-Name') or get_site_name(request.host)
	frappe.init(site=site, sites_path=_sites_path)

	if not (frappe.local.conf and frappe.local.conf.db_name):
		# site does not exist
		raise NotFound

	if frappe.local.conf.get('maintenance_mode'):
		frappe.connect()
		raise frappe.SessionStopped('Session Stopped')
	else:
		frappe.connect(set_admin_as_user=False)

	request.max_content_length = frappe.local.conf.get('max_file_size') or 10 * 1024 * 1024

	make_form_dict(request)

	if request.method != "OPTIONS":
		frappe.local.http_request = frappe.auth.HTTPRequest()

def log_request(request, response):
	if hasattr(frappe.local, 'conf') and frappe.local.conf.enable_frappe_logger:
		frappe.logger("frappe.web", allow_site=frappe.local.site).info({
			"site": get_site_name(request.host),
			"remote_addr": getattr(request, "remote_addr", "NOTFOUND"),
			"base_url": getattr(request, "base_url", "NOTFOUND"),
			"full_path": getattr(request, "full_path", "NOTFOUND"),
			"method": getattr(request, "method", "NOTFOUND"),
			"scheme": getattr(request, "scheme", "NOTFOUND"),
			"http_status_code": getattr(response, "status_code", "NOTFOUND")
		})


def process_response(response):
	if not response:
		return

	# set cookies
	if hasattr(frappe.local, 'cookie_manager'):
		frappe.local.cookie_manager.flush_cookies(response=response)

	# rate limiter headers
	if hasattr(frappe.local, 'rate_limiter'):
		response.headers.extend(frappe.local.rate_limiter.headers())

	# CORS headers
	if hasattr(frappe.local, 'conf') and frappe.conf.allow_cors:
		set_cors_headers(response)

def set_cors_headers(response):
	origin = frappe.request.headers.get('Origin')
	allow_cors = frappe.conf.allow_cors
	if not (origin and allow_cors):
		return

	if allow_cors != "*":
		if not isinstance(allow_cors, list):
			allow_cors = [allow_cors]

		if origin not in allow_cors:
			return

	response.headers.extend({
		'Access-Control-Allow-Origin': origin,
		'Access-Control-Allow-Credentials': 'true',
		'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
		'Access-Control-Allow-Headers': ('Authorization,DNT,X-Mx-ReqToken,'
			'Keep-Alive,User-Agent,X-Requested-With,If-Modified-Since,'
			'Cache-Control,Content-Type')
	})

def make_form_dict(request):
	import json

	request_data = request.get_data(as_text=True)
	if 'application/json' in (request.content_type or '') and request_data:
		args = json.loads(request_data)
	else:
		args = request.form or request.args

	if not isinstance(args, dict):
		frappe.throw(_("Invalid request arguments"))

	try:
		frappe.local.form_dict = frappe._dict({
			k: v[0] if isinstance(v, (list, tuple)) else v for k, v in args.items()
		})
	except IndexError:
		frappe.local.form_dict = frappe._dict(args)

	if "_" in frappe.local.form_dict:
		# _ is passed by $.ajax so that the request is not cached by the browser. So, remove _ from form_dict
		frappe.local.form_dict.pop("_")

def handle_exception(e):
	response = None
	http_status_code = getattr(e, "http_status_code", 500)
	return_as_message = False
	accept_header = frappe.get_request_header("Accept") or ""
	respond_as_json = (
		frappe.get_request_header('Accept')
		and (frappe.local.is_ajax or 'application/json' in accept_header)
		or (
			frappe.local.request.path.startswith("/api/") and not accept_header.startswith("text")
		)
	)

	if frappe.conf.get('developer_mode'):
		# don't fail silently
		print(frappe.get_traceback())

	if respond_as_json:
		# handle ajax responses first
		# if the request is ajax, send back the trace or error message
		response = frappe.utils.response.report_error(http_status_code)

	elif (http_status_code==500
		and (frappe.db and isinstance(e, frappe.db.InternalError))
		and (frappe.db and (frappe.db.is_deadlocked(e) or frappe.db.is_timedout(e)))):
			http_status_code = 508

	elif http_status_code==401:
		frappe.respond_as_web_page(_("Session Expired"),
			_("Your session has expired, please login again to continue."),
			http_status_code=http_status_code,  indicator_color='red')
		return_as_message = True

	elif http_status_code==403:
		frappe.respond_as_web_page(_("Not Permitted"),
			_("You do not have enough permissions to complete the action"),
			http_status_code=http_status_code,  indicator_color='red')
		return_as_message = True

	elif http_status_code==404:
		frappe.respond_as_web_page(_("Not Found"),
			_("The resource you are looking for is not available"),
			http_status_code=http_status_code,  indicator_color='red')
		return_as_message = True

	elif http_status_code == 429:
		response = frappe.rate_limiter.respond()

	else:
		traceback = "<pre>" + sanitize_html(frappe.get_traceback()) + "</pre>"
		# disable traceback in production if flag is set
		if frappe.local.flags.disable_traceback and not frappe.local.dev_server:
			traceback = ""

		frappe.respond_as_web_page("Server Error",
			traceback, http_status_code=http_status_code,
			indicator_color='red', width=640)
		return_as_message = True

	if e.__class__ == frappe.AuthenticationError:
		if hasattr(frappe.local, "login_manager"):
			frappe.local.login_manager.clear_cookies()

	if http_status_code >= 500:
		make_error_snapshot(e)

	if return_as_message:
		response = get_response("message", http_status_code=http_status_code)

	return response

def after_request(rollback):
	if (frappe.local.request.method in ("POST", "PUT") or frappe.local.flags.commit) and frappe.db:
		if frappe.db.transaction_writes:
			frappe.db.commit()
			rollback = False

	# update session
	if getattr(frappe.local, "session_obj", None):
		updated_in_db = frappe.local.session_obj.update()
		if updated_in_db:
			frappe.db.commit()
			rollback = False

	update_comments_in_parent_after_request()

	return rollback

application = local_manager.make_middleware(application)

def serve(port=8000, profile=False, no_reload=False, no_threading=False, site=None, sites_path='.'):
	global application, _site, _sites_path
	_site = site
	_sites_path = sites_path

	from werkzeug.serving import run_simple
	patch_werkzeug_reloader()

	if profile or os.environ.get('USE_PROFILER'):
		application = ProfilerMiddleware(application, sort_by=('cumtime', 'calls'))

	if not os.environ.get('NO_STATICS'):
		application = SharedDataMiddleware(application, {
			str('/assets'): str(os.path.join(sites_path, 'assets'))
		})

		application = StaticDataMiddleware(application, {
			str('/files'): str(os.path.abspath(sites_path))
		})

	application.debug = True
	application.config = {
		'SERVER_NAME': 'localhost:8000'
	}

	log = logging.getLogger('werkzeug')
	log.propagate = False

	in_test_env = os.environ.get('CI')
	if in_test_env:
		log.setLevel(logging.ERROR)

	run_simple('0.0.0.0', int(port), application,
		use_reloader=False if in_test_env else not no_reload,
		use_debugger=not in_test_env,
		use_evalex=not in_test_env,
		threaded=not no_threading)

def patch_werkzeug_reloader():
	"""
	This function monkey patches Werkzeug reloader to ignore reloading files in
	the __pycache__ directory.

	To be deprecated when upgrading to Werkzeug 2.
	"""

	from werkzeug._reloader import WatchdogReloaderLoop

	trigger_reload = WatchdogReloaderLoop.trigger_reload

	def custom_trigger_reload(self, filename):
		if os.path.basename(os.path.dirname(filename)) == "__pycache__":
			return

		return trigger_reload(self, filename)

	WatchdogReloaderLoop.trigger_reload = custom_trigger_reload
