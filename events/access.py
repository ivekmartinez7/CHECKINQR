import hashlib
from functools import wraps

from django.conf import settings
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt, csrf_protect

LOGIN_WINDOW = 300
LOGIN_ATTEMPTS = 12


def login_attempt_allowed(request, username):
    ip = request.META.get('REMOTE_ADDR', '')
    if settings.IS_RENDER:
        # Render appends the address of its direct client at the right.
        ip = request.headers.get('X-Forwarded-For', ip).rsplit(',', 1)[-1].strip()
    identity = f'{ip}:{str(username).strip().casefold()[:254]}'
    key = 'checkin-login:' + hashlib.sha256(identity.encode()).hexdigest()
    if cache.add(key, 1, LOGIN_WINDOW):
        return True
    try:
        return cache.incr(key) <= LOGIN_ATTEMPTS
    except ValueError:
        return cache.add(key, 1, LOGIN_WINDOW)


class CheckInLoginView(LoginView):
    def post(self, request, *args, **kwargs):
        if not login_attempt_allowed(request, request.POST.get('username', '')):
            response = HttpResponse('Espera unos minutos antes de volver a iniciar sesión.', status=429)
            response['Retry-After'] = str(LOGIN_WINDOW)
            return response
        return super().post(request, *args, **kwargs)


def mobile_json(view):
    """Maintain native JSON requests and enforce CSRF on browser-originated writes."""
    protected = csrf_protect(view)

    @csrf_exempt
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return view(request, *args, **kwargs)
        if request.headers.get('Origin') or request.headers.get('Sec-Fetch-Site'):
            return protected(request, *args, **kwargs)
        if request.content_type != 'application/json':
            return JsonResponse({'success': False, 'message': 'Usa application/json.'}, status=415)
        return view(request, *args, **kwargs)

    return wrapped
