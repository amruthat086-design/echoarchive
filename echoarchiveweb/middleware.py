from .views import query


class SessionCheck:
    """Keep the login in step with the database.

    The login is stored in a cookie. If the database is rebuilt, or a user is blocked or removed,
    an old cookie could otherwise show one person's name with another person's permissions.
    On every page we check the user in MySQL and log out anyone who no longer matches.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        uid = request.session.get('uid')
        if uid:
            try:
                u = query("SELECT name, role, status FROM users WHERE uid=%s", (uid,), one=True)
                checked = True
            except Exception:
                checked = False   # database not reachable: let the page show its own error
            if checked:
                if u is None or u['status'] != 'Active':
                    request.session.flush()
                else:
                    if request.session.get('uname') != u['name']:
                        request.session['uname'] = u['name']
                    if request.session.get('urole') != u['role']:
                        request.session['urole'] = u['role']
        return self.get_response(request)
