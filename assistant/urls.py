from django.urls import path
from .views import ChatView, EncryptionKeyView, SyncMessagesView

urlpatterns = [
    path('chat/', ChatView.as_view(), name='assistant-chat'),
    path('encryption-key/', EncryptionKeyView.as_view(), name='assistant-encryption-key'),
    path('sync/messages/', SyncMessagesView.as_view(), name='assistant-sync-messages'),
]
