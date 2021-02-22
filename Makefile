# Rdiffweb
#
# Copyright (C) 2021 IKUS Software inc. All rights reserved.
# IKUS Software inc. PROPRIETARY/CONFIDENTIAL.
# Use is subject to license terms.
#
# Targets:
#    
#    test: 		Run the tests for all components.
#
#    build:		Generate distribution packages for all components 
#
# Define the distribution to be build: buster, stretch, sid, etc.
SHELL = /bin/sh
DIST ?= debian:buster

# To reduce build speed let use an image with python already installed
ifeq ($(DIST),debian:stretch)
DOCKER_IMAGE = ikus060/python:debian9-py3
else ifeq ($(DIST),debian:buster)
DOCKER_IMAGE = ikus060/python:debian10-py3
else ifeq ($(DIST),debian:bullseye)
DOCKER_IMAGE = ikus060/python:debian11-py3
else ifeq ($(DIST),centos:7)
DOCKER_IMAGE = ikus060/python:centos7-py3
else ifeq ($(DIST),centos:8)
DOCKER_IMAGE = ikus060/python:centos8-py3
else
DOCKER_IMAGE = ${DIST}
# TODO Define extra dependencies to be installed
endif

# Check if running in gitlab CICD
define docker_run
docker run -i --rm -v=`pwd`:/build/ -w=/build/ $(1) bash -c "$(2)"
endef

# Retrieve version codename
VERSION_CODENAME = $(shell docker run -i --rm ${DOCKER_IMAGE} bash -c "cat /etc/apt/sources.list | grep '^deb' | head -n 1| cut -d' ' -f3")

# Release date for Debian package
RELEASE_DATE = $(shell date '+%a, %d %b %Y %H:%M:%S') +0000

UID = $(shell id -u)

#
# == Main targets ==
#

all: test bdist clean

test:
	$(call docker_run,${DOCKER_IMAGE},apt update && apt -y install rdiff-backup libldap2-dev libsasl2-dev --no-install-recommends && tox)

bdist:
	$(call docker_run,${DOCKER_IMAGE},apt update && apt -y install rdiff-backup libldap2-dev libsasl2-dev --no-install-recommends && python3 setup.py nosetests --xunit-file=nosetests-$${VERSION_CODENAME}.xml --xunit-testsuite-name=$${VERSION_CODENAME} --cover-xml-file=coverage-$${VERSION_CODENAME}.xml)

bindeb-pkg: 
	$(call docker_run,${DOCKER_IMAGE},apt update && apt install -y devscripts && apt build-dep -y . && dch -v \"$$(python3 setup.py --version)\" \"automated build\" && dpkg-buildpackage -b && mv ../rdiffweb*.deb .)

clean:
	$(call docker_run,${DOCKER_IMAGE},rm -Rf debian/changelog .tox .eggs .coverage coverage*.xml nosetests*.xml)

version:
	@echo ${VERSION}

.PHONY: all test bdist 

