from django.urls import path
from .views import AlertsView, ChatView, EncryptionKeyView, QuickQuestionsView, SyncMessagesView

urlpatterns = [
    path('chat/', ChatView.as_view(), name='assistant-chat'),
    path('alerts/', AlertsView.as_view(), name='assistant-alerts'),
    path('quick-questions/', QuickQuestionsView.as_view(), name='assistant-quick-questions'),
    path('encryption-key/', EncryptionKeyView.as_view(), name='assistant-encryption-key'),
    path('sync/messages/', SyncMessagesView.as_view(), name='assistant-sync-messages'),
]
