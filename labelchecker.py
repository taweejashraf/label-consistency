# Checks for inconsistencies in docker-compose labels
# Dependencies: sudo apt-get install python3-kafka

import urllib.request
import yaml
import time
import re
import os
import hashlib
import json
import sys

try:
	import kafka
except:
	print("Warning: no kafka support")

def autosearch_github(project, path):
	f = urllib.request.urlopen("https://api.github.com/search/code?q={}+in:path+org:{}&type=Code".format(path, project))
	s = f.read().decode("utf-8")
	f.close()

	composefiles = []
	allresults = json.loads(s)
	results = allresults["items"]
	for result in results:
		if result["name"].endswith("docker-compose.yml") or (result["name"].endswith(".yml") and "docker-compose" in result["name"]):
			path = result["repository"]["html_url"] + "/blob/master/" + result["path"]
			composefiles.append(path)
	return composefiles

def normalise_githuburl(url):
	m = re.match(r"^https://github.com/(.+)/blob/(.+)", url)
	if len(m.groups()) == 2:
		url = "https://raw.githubusercontent.com/{}/{}".format(m.groups()[0], m.groups()[1])
	return url

def loadcache():
	cachefiles = {}
	if os.path.isdir("_cache"):
		st = os.stat("_cache/entries")
		cacheage = time.time() - st.st_mtime
		if cacheage < 0 or cacheage > 3600:
			print("discard cache...")
			return cachefiles
		print("loading cache...")
		f = open("_cache/entries")
		cachedfiles = f.readlines()
		f.close()
		for cachedfile in cachedfiles:
			cachedfile = cachedfile[:-1]
			unique = hashlib.md5(cachedfile.encode("utf-8")).hexdigest()
			cachefiles[cachedfile] = "_cache/{}.cache".format(unique)
	return cachefiles

def loading(cachefiles, composefiles):
	print("loading compose files...", end="", flush=True)
	contents = {}
	for composefile in composefiles:
		composefile_load = normalise_githuburl(composefile)
		if composefile in cachefiles:
			composefile_load = cachefiles[composefile]
		if composefile_load in cachefiles:
			composefile_load = cachefiles[composefile_load]
		if "http" in composefile_load:
			f = urllib.request.urlopen(composefile_load)
			s = f.read().decode("utf-8")
		else:
			f = open(composefile_load)
			s = f.read()
		contents[composefile] = s
		if not composefile in cachefiles:
			os.makedirs("_cache", exist_ok=True)
			f = open("_cache/entries", "a")
			print(composefile, file=f)
			f.close()
			unique = hashlib.md5(composefile.encode("utf-8")).hexdigest()
			f = open("_cache/{}.cache".format(unique), "w")
			f.write(s)
			f.close()
		print(".", end="", flush=True)
	print()
	return contents

def consistencycheck(contents):
	print("checking consistency...")

	numservices = 0
	alltags = {}
	faulty = {}

	for content in contents:
		print(content)
		contentname = "faulty:" + content.split("/")[4]
		faulty[contentname] = 0.0

		c = yaml.load(contents[content])
		if not "services" in c:
			print("! no services found")
			faulty[contentname] = faulty.get(contentname, 0) + 1
			continue
		for service in c["services"]:
			print("- service:", service)
			numservices += 1
			if not "labels" in c["services"][service]:
				print("  ! no labels found")
				faulty[contentname] = faulty.get(contentname, 0) + 1
				continue
			for labelpair in c["services"][service]["labels"]:
				print("  - label:", labelpair)
				label, value = labelpair.split("=")
				alltags[label] = alltags.get(label, 0) + 1

	return numservices, alltags, faulty

def sendmessage(host, label, series, message):
	if kafka.__version__.startswith("0"):
		c = kafka.client.KafkaClient(hosts=[host])
		if series:
			p = kafka.producer.keyed.KeyedProducer(c)
		else:
			p = kafka.producer.simple.SimpleProducer(c)
	else:
		p = kafka.KafkaProducer(bootstrap_servers=host)
	success = False
	t = 0.2
	while not success:
		try:
			if kafka.__version__.startswith("0"):
				if series:
					p.send_messages(label, series.encode("utf-8"), message.encode("utf-8"))
				else:
					p.send_messages(label, message.encode("utf-8"))
			else:
				p.send(label, key=series.encode("utf-8"), value=message.encode("utf-8"))
				p.close()
			print("success")
			success = True
		except Exception as e:
			print("error (sleep {})".format(t), e)
			time.sleep(t)
			t *= 2

def labelchecker(autosearch, filebased, eventing):
	composefiles = []

	d_start = time.time()
	cachefiles = loadcache()

	if filebased:
		f = open(filebased)
		composefiles += [line.strip() for line in f.readlines()]

	if autosearch:
		if not cachefiles:
			org, basepath = autosearch.split("/")
			composefiles = autosearch_github(org, basepath)
		else:
			composefiles = cachefiles

	contents = loading(cachefiles, composefiles)
	numservices, alltags, faulty = consistencycheck(contents)
	d_end = time.time()

	print("services: {}".format(numservices))
	print("labels:")
	for label in alltags:
		print("- {}: {} ({:.1f}% coverage)".format(label, alltags[label], 100 * alltags[label] / numservices))
	print("time: {:.1f}s".format(d_end - d_start))

	d = {}
	d["agent"] = "sentinel-generic-agent"
	d["services"] = float(numservices)
	for label in alltags:
		d[label] = float(alltags[label])
	d.update(faulty)
	if eventing:
		kafka, space, series = eventing.split("/")
		print("sending message... {}".format(d))
		sendmessage(kafka, space, series, json.dumps(d))
	else:
		print("not sending message... {}".format(d))

if len(sys.argv) == 1:
	print("Syntax: {} [-a <org>/<basepath>] [-f <file>] [-e <kafka>/<space>/<series>]".format(sys.argv[0]), file=sys.stderr)
	print(" -a: autosearch; find appropriate compose files on GitHub")
	print(" -f: filebased; load paths or URLs as lines from a text file")
	print(" -e: eventing; send results to Kafka endpoint with space and series selection")
	print("Example: {} -a elastest/deploy -e kafka.cloudlab.zhaw.ch/user-1-docker_label_consistency/nightly".format(sys.argv[0]))
	sys.exit(1)

autosearch = None
filebased = None
eventing = None

i = 1
while i < len(sys.argv):
	if sys.argv[i] == "-a":
		autosearch = sys.argv[i + 1]
	elif sys.argv[i] == "-f":
		filebased = sys.argv[i + 1]
	elif sys.argv[i] == "-e":
		eventing = sys.argv[i + 1]
		if not "kafka" in dir():
			print("warning: eventing disabled")
			eventing = None
	i += 1

labelchecker(autosearch, filebased, eventing)
