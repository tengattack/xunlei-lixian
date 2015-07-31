
__all__ = ['download_tool', 'get_tool']

from lixian_config import *
import subprocess
import urllib2
import os.path
import re
import json
import time
import sys

download_tools = {}

def download_tool(name):
	def register(tool):
		download_tools[name] = tool_adaptor(tool)
		return tool
	return register

class DownloadToolAdaptor:
	def __init__(self, tool, **kwargs):
		self.tool = tool
		self.client = kwargs['client']
		self.url = kwargs['url']
		self.path = kwargs['path']
		self.resuming = kwargs.get('resuming')
		self.size = kwargs['size']
	def finished(self):
		assert os.path.getsize(self.path) <= self.size, 'existing file (%s) bigger than expected (%s)' % (os.path.getsize(self.path), self.size)
		return os.path.getsize(self.path) == self.size
	def __call__(self):
		self.tool(self.client, self.url, self.path, self.resuming)

def tool_adaptor(tool):
	import types
	if type(tool) == types.FunctionType:
		def adaptor(**kwargs):
			return DownloadToolAdaptor(tool, **kwargs)
		return adaptor
	else:
		return tool


def check_bin(bin):
	import distutils.spawn
	assert distutils.spawn.find_executable(bin), "Can't find %s" % bin

@download_tool('urllib2')
def urllib2_download(client, download_url, filename, resuming=False):
	'''In the case you don't even have wget...'''
	assert not resuming
	print 'Downloading', download_url, 'to', filename, '...'
	request = urllib2.Request(download_url, headers={'Cookie': 'gdriveid='+client.get_gdriveid()})
	response = urllib2.urlopen(request)
	import shutil
	with open(filename, 'wb') as output:
		shutil.copyfileobj(response, output)

@download_tool('asyn')
def asyn_download(client, download_url, filename, resuming=False):
	import lixian_download_asyn
	lixian_download_asyn.download(download_url, filename, headers={'Cookie': 'gdriveid='+str(client.get_gdriveid())}, resuming=resuming)

@download_tool('wget')
def wget_download(client, download_url, filename, resuming=False):
	gdriveid = str(client.get_gdriveid())
	wget_opts = ['wget', '--header=Cookie: gdriveid='+gdriveid, download_url, '-O', filename]
	if resuming:
		wget_opts.append('-c')
	wget_opts.extend(get_config('wget-opts', '').split())
	check_bin(wget_opts[0])
	exit_code = subprocess.call(wget_opts)
	if exit_code != 0:
		raise Exception('wget exited abnormally')

@download_tool('curl')
def curl_download(client, download_url, filename, resuming=False):
	gdriveid = str(client.get_gdriveid())
	curl_opts = ['curl', '-L', download_url, '--cookie', 'gdriveid='+gdriveid, '--output', filename]
	if resuming:
		curl_opts += ['--continue-at', '-']
	curl_opts.extend(get_config('curl-opts', '').split())
	check_bin(curl_opts[0])
	exit_code = subprocess.call(curl_opts)
	if exit_code != 0:
		raise Exception('curl exited abnormally')

@download_tool('aria2')
@download_tool('aria2c')
class Aria2DownloadTool:
	def __init__(self, **kwargs):
		self.gdriveid = str(kwargs['client'].get_gdriveid())
		self.url = kwargs['url']
		self.path = kwargs['path']
		self.size = kwargs['size']
		self.resuming = kwargs.get('resuming')
	def finished(self):
		assert os.path.getsize(self.path) <= self.size, 'existing file (%s) bigger than expected (%s)' % (os.path.getsize(self.path), self.size)
		return os.path.getsize(self.path) == self.size and not os.path.exists(self.path + '.aria2')
	def __call__(self):
		gdriveid = self.gdriveid
		download_url = self.url
		path = self.path
		resuming = self.resuming
		dir = os.path.dirname(path)
		filename = os.path.basename(path)
		aria2_opts = ['aria2c', '--header=Cookie: gdriveid='+gdriveid, download_url, '--out', filename, '--file-allocation=none']
		if dir:
			aria2_opts.extend(('--dir', dir))
		if resuming:
			aria2_opts.append('-c')
		aria2_opts.extend(get_config('aria2-opts', '').split())
		check_bin(aria2_opts[0])
		exit_code = subprocess.call(aria2_opts)
		if exit_code != 0:
			raise Exception('aria2c exited abnormally')

@download_tool('aria2-rpc')
class Aria2RPCDownloadTool:
	def __init__(self, **kwargs):
		self.gdriveid = str(kwargs['client'].get_gdriveid())
		self.url = kwargs['url']
		self.path = kwargs['path']
		self.size = kwargs['size']
		self.resuming = kwargs.get('resuming')
		self.jsonrpcUrl = get_config('aria2-rpc-opts', '')
	def finished(self):
		assert os.path.getsize(self.path) <= self.size, 'existing file (%s) bigger than expected (%s)' % (os.path.getsize(self.path), self.size)
		return os.path.getsize(self.path) == self.size and not os.path.exists(self.path + '.aria2')
	def generate_id(self):
		return int(time.time() * 1000)
	def request(self, method, id, params):
		rpc_opts = {
			"jsonrpc": "2.0",
			"method": method,
			"id": id,
			"params": params
			}
		data = json.dumps(rpc_opts)
		resp = urllib2.urlopen(self.jsonrpcUrl, data, timeout=60)
		str = resp.read()
		r = json.loads(str)
		return r['result']
	def add_task(self, id, download_url, filename):
		gdriveid = self.gdriveid
		r = self.request('aria2.addUri', id, [
				[ download_url ],
				{ 'out': filename,
				  'header': 'Cookie: gdriveid='+gdriveid,
				}
			])
		return r
	def wait_download(self, id, gid):
		print 'Start downloading...'
		while True:
			time.sleep(1)
			r = self.request('aria2.tellStatus', id, [ gid, [ 'gid', 'status', 'downloadSpeed', 'completedLength', 'totalLength' ] ])
			completed = int(r['completedLength'])
			total = int(r['totalLength'])
			if total:
				percent = completed * 100.0 / total
			else:
				percent = 0.0
			bar = '%.1f%% %s %s/%s' % (
				percent,
				self.human_speed(int(r['downloadSpeed'])),
				r['completedLength'],
				r['totalLength'])
			sys.stdout.write('\r'+bar)
			sys.stdout.flush()
			if r['status'] != 'active':
				sys.stdout.write('\n')
				# may paused or stopped unexpectedly
				return r['status']
	def human_speed(self, speed):
		if speed < 1000:
			speed = '%sB/s' % int(speed)
		elif speed < 1000*10:
			speed = '%.1fK/s' % (speed/1000.0)
		elif speed < 1000*1000:
			speed = '%dK/s' % int(speed/1000)
		elif speed < 1000*1000*100:
			speed = '%.1fM/s' % (speed/1000.0/1000.0)
		else:
			speed = '%dM/s' % int(speed/1000/1000)
		return speed
	def __call__(self):
		path = self.path
		resuming = self.resuming
		#dir = os.path.dirname(path)
		filename = os.path.basename(path)

		assert re.match(r'[^:]+', self.jsonrpcUrl), 'Invalid jsonrpc URL in aria2-rpc-opts: ' + jsonrpcUrl

		id = self.generate_id()
		gid = self.add_task(id, self.url, filename)
		status = self.wait_download(id, gid)
		if status != 'complete':
			raise Exception('aria2-rpc task ' + status)

@download_tool('axel')
def axel_download(client, download_url, path, resuming=False):
	gdriveid = str(client.get_gdriveid())
	axel_opts = ['axel', '--header=Cookie: gdriveid='+gdriveid, download_url, '--output', path]
	axel_opts.extend(get_config('axel-opts', '').split())
	check_bin(axel_opts[0])
	exit_code = subprocess.call(axel_opts)
	if exit_code != 0:
		raise Exception('axel exited abnormally')

def get_tool(name):
	return download_tools[name]
