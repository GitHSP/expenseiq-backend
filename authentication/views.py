# authentication/views.py

from rest_framework             import status
from rest_framework.response    import Response
from rest_framework.views       import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken

from django.contrib.auth        import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding      import force_bytes
from django.utils.http          import urlsafe_base64_encode, urlsafe_base64_decode
from django.core.mail           import send_mail
from django.conf                import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.cache          import cache
from django.core.exceptions     import ValidationError as DjangoValidationError

from . import two_factor
from .serializers import RegisterSerializer, UserSerializer

User = get_user_model()


class RegisterView(APIView):
    """
    POST /api/auth/register/
    Creates a new user account
    No authentication required
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            # Generate tokens immediately after registration
            refresh = RefreshToken.for_user(user)
            return Response({
                'user':          UserSerializer(user).data,
                'access_token':  str(refresh.access_token),
                'refresh_token': str(refresh),
            }, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# Failed logins per account before it's locked for a while. Counted per
# email (not per address), so spreading guesses across IPs doesn't help.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60


def _lockout_key(email):
    return f"login-failures:{email.strip().lower()}"


def revoke_refresh_tokens(user):
    """Sign the user out everywhere: blacklist every refresh token issued to
    them. Access tokens are short-lived, so sessions end within minutes."""
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)


class LoginView(APIView):
    """
    POST /api/auth/login/
    Logs in with email + password
    Returns JWT tokens
    No authentication required
    """
    permission_classes = [AllowAny]

    def post(self, request):
        email    = (request.data.get('email') or '').strip()
        password = request.data.get('password') or ''

        # Validate inputs
        if not email or not password:
            return Response(
                {'error': 'Please provide email and password'},
                status=status.HTTP_400_BAD_REQUEST
            )

        key = _lockout_key(email)
        if cache.get(key, 0) >= LOGIN_MAX_FAILURES:
            return Response(
                {'error': 'Too many failed attempts. Try again in 15 minutes, or reset your password.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        user = User.objects.filter(email__iexact=email).order_by('id').first()
        if user is None:
            # Spend the same time hashing as a real check, so response times
            # don't reveal which emails have accounts.
            make_password(password)
        if user is None or not user.check_password(password):
            cache.add(key, 0, LOGIN_LOCKOUT_SECONDS)
            cache.incr(key)
            return Response(
                {'error': 'Invalid email or password'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Check account is active
        if not user.is_active:
            return Response(
                {'error': 'Account is disabled'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        cache.delete(key)

        # With two-factor on, the password alone isn't enough: hand back a
        # short-lived token for the code step instead of a session.
        if two_factor.is_enabled(user):
            return Response({
                'mfa_required': True,
                'mfa_token':    two_factor.make_mfa_token(user),
            })
        return Response(_session_for(user))


def _session_for(user):
    refresh = RefreshToken.for_user(user)
    return {
        'user':          UserSerializer(user).data,
        'access_token':  str(refresh.access_token),
        'refresh_token': str(refresh),
    }


class LoginTwoFactorView(APIView):
    """
    POST /api/auth/login/2fa/  {"mfa_token", "code"}
    Second login step: a 6-digit authenticator code or a backup code.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        user_id = two_factor.read_mfa_token(request.data.get('mfa_token'))
        user = User.objects.filter(pk=user_id, is_active=True).first() if user_id else None
        if user is None or not two_factor.is_enabled(user):
            return Response({'error': 'Your sign-in expired. Please enter your password again.'},
                            status=status.HTTP_401_UNAUTHORIZED)
        if two_factor.is_locked(user):
            return Response({'error': 'Too many wrong codes. Try again in 15 minutes.'},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        ok, used_backup = two_factor.verify(user, request.data.get('code'))
        if not ok:
            return Response({'error': 'That code is incorrect or has already been used.'},
                            status=status.HTTP_401_UNAUTHORIZED)
        data = _session_for(user)
        if used_backup:
            data['backup_codes_left'] = len(user.two_factor.backup_code_hashes)
        return Response(data)


class TwoFactorView(APIView):
    """
    GET    /api/auth/2fa/          status
    POST   /api/auth/2fa/          start setup -> QR code + key (not active yet)
    PUT    /api/auth/2fa/          {"code"} confirm setup -> turns it on, returns backup codes
    DELETE /api/auth/2fa/          {"password", "code"} turn it off
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tf = getattr(request.user, 'two_factor', None)
        return Response({
            'enabled':           bool(tf and tf.enabled),
            'enabled_at':        tf.enabled_at if tf and tf.enabled else None,
            'backup_codes_left': len(tf.backup_code_hashes) if tf and tf.enabled else 0,
        })

    def post(self, request):
        if two_factor.is_enabled(request.user):
            return Response({'error': 'Two-factor login is already on.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(two_factor.start_setup(request.user))

    def put(self, request):
        if two_factor.is_locked(request.user):
            return Response({'error': 'Too many wrong codes. Try again in 15 minutes.'},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        codes = two_factor.confirm_setup(request.user, request.data.get('code'))
        if codes is None:
            return Response({'error': "That code didn't match. Check your phone's time is set automatically and try the newest code."},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'enabled': True, 'backup_codes': codes})

    def delete(self, request):
        user = request.user
        if not two_factor.is_enabled(user):
            return Response({'error': 'Two-factor login is already off.'}, status=status.HTTP_400_BAD_REQUEST)
        if not user.check_password(request.data.get('password') or ''):
            return Response({'error': 'Your password is incorrect.'}, status=status.HTTP_400_BAD_REQUEST)
        ok, _ = two_factor.verify(user, request.data.get('code'))
        if not ok:
            return Response({'error': 'That code is incorrect.'}, status=status.HTTP_400_BAD_REQUEST)
        user.two_factor.delete()
        return Response({'enabled': False})


class TwoFactorBackupCodesView(APIView):
    """POST /api/auth/2fa/backup-codes/ {"code"} — replace all backup codes."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if not two_factor.is_enabled(user):
            return Response({'error': 'Turn on two-factor login first.'}, status=status.HTTP_400_BAD_REQUEST)
        ok, _ = two_factor.verify(user, request.data.get('code'), allow_backup=False)
        if not ok:
            return Response({'error': 'That code is incorrect.'}, status=status.HTTP_400_BAD_REQUEST)
        codes, hashes = two_factor.new_backup_codes()
        user.two_factor.backup_code_hashes = hashes
        user.two_factor.save(update_fields=['backup_code_hashes'])
        return Response({'backup_codes': codes})


class LogoutView(APIView):
    """
    POST /api/auth/logout/
    Blacklists the refresh token so it can't be reused
    Requires authentication
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            refresh_token = request.data.get('refresh_token')
            token = RefreshToken(refresh_token)
            token.blacklist()
            return Response({'message': 'Logged out successfully'})
        except Exception:
            return Response(
                {'error': 'Invalid token'},
                status=status.HTTP_400_BAD_REQUEST
            )


class LogoutAllView(APIView):
    """POST /api/auth/logout-all/ — end every session on every device."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        revoke_refresh_tokens(request.user)
        return Response({'message': 'Signed out everywhere'})


class MeView(APIView):
    """
    GET /api/auth/me/
    Returns current logged in user data
    Requires authentication
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class ForgotPasswordView(APIView):
    """
    POST /api/auth/forgot-password/
    Sends a password reset email
    No authentication required
    """
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get('email')
        try:
            user = User.objects.get(email__iexact=(email or '').strip())
            # Generate reset token
            token = default_token_generator.make_token(user)
            uid   = urlsafe_base64_encode(force_bytes(user.pk))
            # Build reset link pointing to React app
            reset_link = f"{settings.FRONTEND_URL}/reset-password/{uid}/{token}/"
            # Send email
            send_mail(
                subject        = 'Glacier — Reset your password',
                message        = f'Click this link to reset your password:\n\n{reset_link}\n\nThis link expires in 24 hours.',
                from_email     = settings.DEFAULT_FROM_EMAIL,
                recipient_list = [user.email],
            )
        except User.DoesNotExist:
            pass  # Don't reveal if email exists — security best practice

        # Always return success to prevent email enumeration attacks
        return Response({
            'message': 'If that email exists a reset link has been sent'
        })


class ResetPasswordView(APIView):
    """
    POST /api/auth/reset-password/
    Resets password using token from email link
    No authentication required
    """
    permission_classes = [AllowAny]

    def post(self, request):
        uid      = request.data.get('uid')
        token    = request.data.get('token')
        password = request.data.get('password')

        try:
            # Decode user ID
            user_id = urlsafe_base64_decode(uid).decode()
            user    = User.objects.get(pk=user_id)

            # Verify token is valid and not expired
            if not default_token_generator.check_token(user, token):
                return Response(
                    {'error': 'Invalid or expired reset link'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            try:
                validate_password(password or '', user=user)
            except DjangoValidationError as e:
                return Response({'error': ' '.join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)

            # Set new password and sign out every existing session.
            user.set_password(password)
            user.save()
            revoke_refresh_tokens(user)
            cache.delete(_lockout_key(user.email))
            return Response({'message': 'Password reset successfully'})

        except (User.DoesNotExist, ValueError, TypeError):
            return Response(
                {'error': 'Invalid reset link'},
                status=status.HTTP_400_BAD_REQUEST
            )


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user          = request.user
        old_password  = request.data.get('old_password')
        new_password  = request.data.get('new_password')
        new_password2 = request.data.get('new_password2')

        if not old_password or not new_password or not new_password2:
            return Response(
                {'error': 'All fields are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if not user.check_password(old_password):
            return Response(
                {'error': 'Current password is incorrect'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if new_password != new_password2:
            return Response(
                {'error': 'New passwords do not match'},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as e:
            return Response({'error': ' '.join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)
        user.set_password(new_password)
        user.save()
        # Sign out everywhere else (a stolen session stops working); this
        # device gets fresh tokens so it stays signed in.
        revoke_refresh_tokens(user)
        refresh = RefreshToken.for_user(user)
        return Response({
            'message':       'Password changed successfully',
            'access_token':  str(refresh.access_token),
            'refresh_token': str(refresh),
        })