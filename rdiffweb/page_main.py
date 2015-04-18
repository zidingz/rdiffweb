#!/usr/bin/python
# -*- coding: utf-8 -*-
# rdiffweb, A web interface to rdiff-backup repositories
# Copyright (C) 2014 rdiffweb contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import cherrypy
import librdiff
import logging
import os.path

from rdw_helpers import encode_s, decode_s

# Define the logger
logger = logging.getLogger(__name__)


class MainPage:

    def validate_user_path(self, path_b):
        '''Takes a path relative to the user's root dir and validates that it
        is valid and within the user's root'''
        assert isinstance(path_b, str)
        path_b = path_b.strip(b"/")

        # NOTE: a blank path is allowed, since the user root directory might be
        # a repository.

        logger.debug("check user access to path [%s]" %
                     decode_s(path_b, 'replace'))

        # Get reference to user repos
        user_repos = self.app.userdb.get_repos(self.get_username())

        # Check if any of the repos matches the given path.
        user_repos_matches = filter(
            lambda x: path_b.startswith(encode_s(x).strip(b"/")),
            user_repos)
        if not user_repos_matches:
            # No repo matches
            logger.error("user doesn't have access to [%s]" %
                         decode_s(path_b, 'replace'))
            raise librdiff.AccessDeniedError
        repo_b = encode_s(user_repos_matches[0]).strip(b"/")

        # Get reference to user_root
        user_root = self.app.userdb.get_root_dir(self.get_username())
        user_root_b = encode_s(user_root)

        # Check path vs real path value
        full_path_b = os.path.join(user_root_b, path_b).rstrip(b"/")
        real_path_b = os.path.realpath(full_path_b).rstrip(b"/")
        if full_path_b != real_path_b:
            # We can safely assume the realpath contains a symbolic link. If
            # the symbolic link is valid, we display the content of the "real"
            # path.
            if real_path_b.startswith(os.path.join(user_root_b, repo_b)):
                path_b = os.path.relpath(real_path_b, user_root_b)
            else:
                logger.warn("access is denied [%s] vs [%s]" % (
                    decode_s(full_path_b, 'replace'),
                    decode_s(real_path_b, 'replace')))
                raise librdiff.AccessDeniedError

        # Get reference to the repository (this ensure the repository does
        # exists and is valid.)
        repo_obj = librdiff.RdiffRepo(user_root_b, repo_b)

        # Get reference to the path.
        path_b = path_b[len(repo_b):]
        path_obj = repo_obj.get_path(path_b)

        return (repo_obj, path_obj)

    def _is_submit(self):
        """
        Check if the cherrypy request is a POST.
        """
        return cherrypy.request.method == "POST"

    def _compile_error_template(self, error):
        """
        Compile an error template.
            `error` the error message.
        """
        assert isinstance(error, unicode)
        return self._compile_template("error.html", error=error)

    def _compile_template(self, template_name, **kwargs):
        """
        Used to generate a standard HTML page using the given template.
        This method should be used by subclasses to provide default template
        value.
        """
        parms = {
            "is_login": True,
            "is_admin": self._user_is_admin(),
            "username": self.get_username(),
            }

        # Append custom branding
        if hasattr(self.app, "favicon"):
            parms["favicon"] = self.app.favicon  # See main,py
        if hasattr(self.app, "header_logo"):
            parms["header_logo"] = self.app.header_logo  # See main,py
        header_name = self.app.config.get_config("HeaderName")
        if header_name:
            parms["header_name"] = header_name

        # Append template parameters.
        parms.update(kwargs)
        return self.app.templates.compile_template(template_name, **parms)

    def __get_app(self):
        """
        Utility method to access the application. (a.k.a. RdiffwebApp)

        Raise a ValueError if the application is not accessible.
        """
        try:
            app = cherrypy.request.app.root  # @UndefinedVariable
        except:
            app = False
        if not app:
            raise ValueError("app is not available")
        return app

    def get_username(self):
        """
        Get the current username (from cherrypy session).
        """
        try:
            return cherrypy.session['username']  # @UndefinedVariable
        except:
            return None

    def set_username(self, username):
        """
        Store the username in the user session.
        """
        cherrypy.session['username'] = username  # @UndefinedVariable

    def _user_is_admin(self):
        """Check if current user is administrator. Return True or False."""
        current_username = self.get_username()
        if current_username:
            try:
                return self.app.userdb.is_admin(current_username)
            except:
                logger.exception("fail to verify if user is admin")
        return False

    app = property(fget=__get_app)