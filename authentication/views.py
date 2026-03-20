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


class LoginView(APIView):
    """
    POST /api/auth/login/
    Logs in with email + password
    Returns JWT tokens
    No authentication required
    """
    permission_classes = [AllowAny]

    def post(self, request):
        email    = request.data.get('email')
        password = request.data.get('password')

        # Validate inputs
        if not email or not password:
            return Response(
                {'error': 'Please provide email and password'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Find user by email
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response(
                {'error': 'Invalid email or password'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Check password
        if not user.check_password(password):
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

        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)
        return Response({
            'user':          UserSerializer(user).data,
            'access_token':  str(refresh.access_token),
            'refresh_token': str(refresh),
        })


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
            user = User.objects.get(email=email)
            # Generate reset token
            token = default_token_generator.make_token(user)
            uid   = urlsafe_base64_encode(force_bytes(user.pk))
            # Build reset link pointing to React app
            reset_link = f"http://localhost:3000/reset-password/{uid}/{token}/"
            # Send email
            send_mail(
                subject        = 'ExpenseIQ — Reset your password',
                message        = f'Click this link to reset your password:\n\n{reset_link}\n\nThis link expires in 24 hours.',
                from_email     = settings.DEFAULT_FROM_EMAIL,
                recipient_list = [email],
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

            # Set new password
            user.set_password(password)
            user.save()
            return Response({'message': 'Password reset successfully'})

        except (User.DoesNotExist, ValueError, TypeError):
            return Response(
                {'error': 'Invalid reset link'},
                status=status.HTTP_400_BAD_REQUEST
            )