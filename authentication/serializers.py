# authentication/serializers.py

from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

User = get_user_model()

class RegisterSerializer(serializers.ModelSerializer):
    """
    Handles new user registration.
    Validates password strength and matching confirmation.
    """
    password  = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True)

    class Meta:
        model  = User
        fields = ['email', 'username', 'password', 'password2']

    def validate(self, data):
        # Check passwords match
        if data['password'] != data['password2']:
            raise serializers.ValidationError({"password": "Passwords do not match"})
        return data

    def create(self, validated_data):
        # Remove password2 before creating user
        validated_data.pop('password2')
        user = User.objects.create_user(
            email    = validated_data['email'],
            username = validated_data['username'],
            password = validated_data['password'],
        )
        return user


class UserSerializer(serializers.ModelSerializer):
    """
    Returns safe user data — never returns password
    """
    class Meta:
        model  = User
        fields = ['id', 'email', 'username']


class ChangePasswordSerializer(serializers.Serializer):
    """
    Handles password change for logged in users
    """
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(
        write_only=True,
        validators=[validate_password]
    )