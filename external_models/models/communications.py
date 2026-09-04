from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from external_models.models.external_references import Lead, Account, Campaign, Funnel
from external_models.models.messages import MessageTemplate
from external_models.models.reporting import BlandAICall
# BulkCampaignMessage: use string ref below to avoid circular import (communications <- channel_configs <- nurturing_campaigns)


POSTMARK_TRACK_LINKS_VALUES = {'None', 'HtmlAndText', 'HtmlOnly', 'TextOnly', 'Subscription'}


def validate_email_provider_config(provider, config):
    cfg = config or {}
    if not isinstance(cfg, dict):
        raise ValueError('Email provider config must be a JSON object')
    if provider == 'postmark':
        raw_track_links = cfg.get('track_links')
        if raw_track_links not in (None, '') and raw_track_links not in POSTMARK_TRACK_LINKS_VALUES:
            raise ValueError(f'postmark track_links must be one of {sorted(POSTMARK_TRACK_LINKS_VALUES)}')
    if provider == 'mailchimp_transactional':
        subaccount = cfg.get('subaccount')
        if subaccount is not None and not isinstance(subaccount, str):
            raise ValueError('mailchimp_transactional "subaccount" must be a string')
        for key in ('track_opens', 'track_clicks'):
            val = cfg.get(key)
            if val is not None and not isinstance(val, bool):
                raise ValueError(f'mailchimp_transactional "{key}" must be a boolean')


class Conversation(models.Model):
    """
    Represents a Twilio Conversation session.
    """
    STATE_CHOICES = (
        ('active', 'Active'),
        ('closed', 'Closed'),
        ('archived', 'Archived'),
    )

    CHANNEL_CHOICES = (
        ('sms', 'SMS'),
        ('chat', 'Chat'),
        ('email', 'Email'),
        ('voice', 'Voice'),
    )

    # The unique Twilio ID for this conversation (e.g. 'CHXXXXXXXXXXXXXXXXX')
    twilio_sid = models.CharField(max_length=34, unique=True)
    
    # Twilio Account SID
    account_sid = models.CharField(max_length=34, blank=True, null=True)
    
    # Twilio Messaging Service SID (if used)
    messaging_service_sid = models.CharField(max_length=34, blank=True, null=True)

    # Friendly name, if you set one via Twilio or want to store a local name
    friendly_name = models.CharField(max_length=255, blank=True, null=True)

    # Conversation state as defined by Twilio; using choices enforces valid values.
    state = models.CharField(max_length=50, choices=STATE_CHOICES, default="active")

    # Default channel for this conversation
    channel = models.CharField(
        max_length=10,
        choices=CHANNEL_CHOICES,
        blank=True,
        null=True,
        help_text="Default channel for this conversation"
    )

    # Link to your internal CRM Lead (if applicable)
    lead = models.ForeignKey(
        Lead,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversations"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Track which user from your team created the conversation (if applicable)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_conversations"
    )

    class Meta:
        managed = False
        db_table = 'communications_conversation'
        indexes = [
            models.Index(fields=['state']),
            models.Index(fields=['friendly_name']),
            models.Index(fields=['account_sid']),
            models.Index(fields=['messaging_service_sid']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"Conversation {self.twilio_sid}"


class Participant(models.Model):
    """
    A participant in a Twilio Conversation (could be an end user, a phone number, etc.).
    """
    # Twilio participant SID (e.g. 'MBXXXXXXXXXXXXXXXXX')
    participant_sid = models.CharField(max_length=34, unique=True)

    # The conversation they belong to
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="participants"
    )

    # For SMS-based participants, store their phone number.
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    
    # Location data for phone numbers
    country = models.CharField(max_length=2, blank=True, null=True)
    state = models.CharField(max_length=50, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    zip_code = models.CharField(max_length=20, blank=True, null=True)

    # If a user on your team is also a participant.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversation_participants"
    )

    # Optional JSON field to store channel-specific metadata (e.g., chat identity, messaging binding)
    metadata = models.JSONField(blank=True, null=True, help_text="Optional channel-specific metadata.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'communications_participant'
        indexes = [
            models.Index(fields=['phone_number']),
            models.Index(fields=['country']),
            models.Index(fields=['state']),
        ]

    def __str__(self):
        return f"Participant {self.participant_sid} in {self.conversation}"


class ConversationMessage(models.Model):
    """
    A message in a Twilio Conversation.
    """
    # Twilio message identifiers
    message_sid = models.CharField(max_length=34, unique=True)
    sms_message_sid = models.CharField(max_length=34, blank=True, null=True)
    sms_sid = models.CharField(max_length=34, blank=True, null=True)
    
    # Twilio Account SID
    account_sid = models.CharField(max_length=34, blank=True, null=True)
    
    # Twilio Messaging Service SID (if used)
    messaging_service_sid = models.CharField(max_length=34, blank=True, null=True)

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages"
    )

    participant = models.ForeignKey(
        Participant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages"
    )

    # The message text body.
    body = models.TextField()

    # Optional field to capture message direction (inbound/outbound) if needed.
    DIRECTION_CHOICES = (
        ('inbound', 'Inbound'),
        ('outbound', 'Outbound'),
    )
    direction = models.CharField(
        max_length=8,
        choices=DIRECTION_CHOICES,
        blank=True,
        null=True,
        help_text="Message direction if applicable."
    )

    # Message status
    status = models.CharField(max_length=50, blank=True, null=True)
    
    # Number of segments for long messages
    num_segments = models.IntegerField(default=1)
    
    # Number of media attachments
    num_media = models.IntegerField(default=0)

    # Optional field to store a media URL if the message includes an attachment.
    media_url = models.URLField(blank=True, null=True, help_text="Optional URL for attached media.")

    CHANNEL_CHOICES = (
        ('sms', 'SMS'),
        ('chat', 'Chat'),
        ('email', 'Email'),
        ('voice', 'Voice'),
    )
    channel = models.CharField(
        max_length=10,
        choices=CHANNEL_CHOICES,
        blank=True,
        null=True,
        help_text="Channel of communication used for this message."
    )

    in_reply_to_bulk_campaign_message = models.ForeignKey(
        'external_models.BulkCampaignMessage',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='conversation_replies',
        help_text="The bulk campaign message this conversation message is replying to (when from nurturing follow-up).",
    )

    # Store the complete raw message data
    raw_data = models.JSONField(blank=True, null=True, help_text="Complete raw message data from Twilio")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = 'communications_conversationmessage'
        indexes = [
            models.Index(fields=['message_sid']),
            models.Index(fields=['sms_message_sid']),
            models.Index(fields=['sms_sid']),
            models.Index(fields=['status']),
        ]
        ordering = ['created_at']

    def __str__(self):
        return f"Message {self.message_sid}: {self.body[:50]}..."


class ConversationThread(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='threads')
    subject = models.CharField(max_length=255, blank=True, null=True)
    channel = models.CharField(
        max_length=10,
        choices=[('sms', 'SMS'), ('email', 'Email'), ('voice', 'Voice'), ('chat', 'Chat')],
        help_text="Platform where the conversation happened."
    )
    status = models.CharField(
        max_length=20,
        choices=[('open', 'Open'), ('assigned', 'Assigned'), ('closed', 'Closed')],
        default='open'
    )
    last_message_timestamp = models.DateTimeField(blank=True, null=True)

    # Link to Twilio Conversation object (optional)
    twilio_conversation = models.ForeignKey(
        Conversation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='threads'
    )

    # Link to Bland AI Call (optional)
    bland_ai_call = models.ForeignKey(
        BlandAICall,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='threads'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'communications_conversationthread'
        ordering = ['-last_message_timestamp']

    def __str__(self):
        return f"Thread {self.id} - {self.lead} ({self.channel})"


class ThreadMessage(models.Model):
    thread = models.ForeignKey(ConversationThread, on_delete=models.CASCADE, related_name='messages')
    sender_type = models.CharField(max_length=10, choices=[('user', 'User'), ('contact', 'Contact')])
    content = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)
    channel = models.CharField(
        max_length=10,
        choices=[('sms', 'SMS'), ('email', 'Email'), ('voice', 'Voice'), ('chat', 'Chat')]
    )
    media_url = models.URLField(blank=True, null=True)
    read_status = models.BooleanField(default=False, help_text="Whether the message has been read")
    replied_status = models.BooleanField(default=False, help_text="Whether the message has been replied to")
    bland_ai_message_id = models.BigIntegerField(null=True, blank=True, unique=True, help_text="ID of the message in Bland AI transcripts")

    # Optional link to raw Twilio message
    twilio_message = models.ForeignKey(
        ConversationMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='thread_messages'
    )

    # Optional link to the lead who sent the message
    lead = models.ForeignKey(
        Lead,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='thread_messages'
    )

    # Optional link to the user who sent the message
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='thread_messages'
    )

    class Meta:
        managed = False
        db_table = 'communications_threadmessage'
        ordering = ['timestamp']

    def __str__(self):
        return f"Message {self.id} in Thread {self.thread.id}"


class ContactEndpointChannel(models.Model):
    CHANNEL_CHOICES = (
        ('sms', 'SMS'),
        ('voice', 'Voice'),
        ('email', 'Email'),
        ('social', 'Social Media'),
    )

    endpoint = models.ForeignKey('ContactEndpoint', on_delete=models.CASCADE, related_name='channels')
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)

    class Meta:
        managed = False
        unique_together = ('endpoint', 'channel')
        db_table = 'contact_endpoint_channel'

    def __str__(self):
        return f"{self.endpoint.value} - {self.channel}"


class ContactEndpointPlatform(models.Model):
    PLATFORM_TYPE_CHOICES = (
        ('phone', 'Phone'),
        ('email', 'Email'),
        ('social', 'Social Media'),
        ('other', 'Other'),
    )

    name = models.CharField(
        max_length=50,
        unique=True,
        help_text="Platform name (e.g. 'Twilio', 'Gmail', 'Facebook')"
    )
    display_name = models.CharField(
        max_length=100,
        help_text="User-friendly display name"
    )
    platform_type = models.CharField(
        max_length=20,
        choices=PLATFORM_TYPE_CHOICES,
        help_text="Category of platform"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this platform is available for use"
    )
    description = models.TextField(
        blank=True,
        null=True,
        help_text="Optional description of the platform"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'contact_endpoint_platform'
        ordering = ['platform_type', 'display_name']

    def __str__(self):
        return self.display_name


class ContactEndpoint(models.Model):
    PRIORITY_CHOICES = (
        ('primary', 'Primary'),
        ('secondary', 'Secondary'),
        ('backup', 'Backup'),
    )

    # Contact info
    value = models.CharField(
        max_length=255,
        help_text="Phone number, email address, or social handle"
    )
    platform = models.ForeignKey(
        ContactEndpointPlatform,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='endpoints',
        help_text="Platform or service provider for this contact endpoint"
    )

    # Enhancements
    label = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="User-friendly label (e.g. 'Work Phone', 'Personal Email')"
    )
    priority = models.CharField(
        max_length=10,
        choices=PRIORITY_CHOICES,
        default='primary',
        help_text="Preferred usage priority for this endpoint"
    )
    is_primary = models.BooleanField(default=False)
    is_verified = models.BooleanField(default=False)

    # CRM Relationships
    account = models.ForeignKey(
        Account,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='contact_endpoints'
    )
    funnel = models.ForeignKey(
        Funnel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='contact_endpoints'
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = 'contact_endpoint'
        indexes = [
            models.Index(fields=['value']),
        ]

    def __str__(self):
        label = f"{self.value}"
        if self.label:
            label += f" ({self.label})"
        elif self.platform:
            label += f" ({self.platform.display_name})"
        return label

    def get_channel_list(self):
        return [c.channel for c in self.channels.all()]

    def get_campaigns(self):
        """
        Get all campaigns this contact endpoint is assigned to.
        Returns a queryset of Campaign objects.
        """
        return Campaign.objects.filter(
            contact_endpoint_mappings__contact_endpoint=self,
            contact_endpoint_mappings__is_active=True
        ).distinct()

    def get_active_campaign_mappings(self):
        """
        Get all active campaign mappings for this contact endpoint.
        Returns a queryset of ContactEndpointCampaign objects.
        """
        return self.campaign_mappings.filter(is_active=True)

    def add_to_campaign(self, campaign):
        """
        Add this contact endpoint to a campaign.
        Creates a new mapping if it doesn't exist, or reactivates if inactive.
        """
        mapping, created = ContactEndpointCampaign.objects.get_or_create(
            contact_endpoint=self,
            campaign=campaign,
            defaults={'is_active': True}
        )
        if not created and not mapping.is_active:
            mapping.is_active = True
            mapping.save()
        return mapping

    def remove_from_campaign(self, campaign):
        """
        Remove this contact endpoint from a campaign.
        Sets the mapping as inactive rather than deleting it.
        """
        try:
            mapping = self.campaign_mappings.get(campaign=campaign)
            mapping.is_active = False
            mapping.save()
            return True
        except ContactEndpointCampaign.DoesNotExist:
            return False


class ContactEndpointEmailSettings(models.Model):
    """
    Email provider configuration for a ContactEndpoint (1:1).
    Non-secret options live in `config`; API keys live in AWS Secrets Manager (ARN on this row).

    Supported providers (see registry): mailgun, postmark and mailchimp_transactional are
    implemented for send. resend and mailchimp_marketing exist in choices but have no send
    adapter here; mailchimp_marketing is audience/campaign-grain and cannot do the
    per-recipient sends this pipeline is built on (use mailchimp_transactional instead).
    """

    PROVIDER_MAILGUN = 'mailgun'
    PROVIDER_POSTMARK = 'postmark'
    PROVIDER_RESEND = 'resend'
    PROVIDER_MAILCHIMP_MARKETING = 'mailchimp_marketing'
    PROVIDER_MAILCHIMP_TRANSACTIONAL = 'mailchimp_transactional'

    PROVIDER_CHOICES = (
        (PROVIDER_MAILGUN, 'Mailgun'),
        (PROVIDER_POSTMARK, 'Postmark'),
        (PROVIDER_RESEND, 'Resend'),
        (PROVIDER_MAILCHIMP_MARKETING, 'Mailchimp Marketing'),
        (PROVIDER_MAILCHIMP_TRANSACTIONAL, 'Mailchimp Transactional (Mandrill)'),
    )

    endpoint = models.OneToOneField(
        'ContactEndpoint',
        on_delete=models.CASCADE,
        related_name='email_settings',
        help_text='Contact endpoint this email configuration applies to',
    )
    provider = models.CharField(max_length=40, choices=PROVIDER_CHOICES)
    config = models.JSONField(
        default=dict,
        blank=True,
        help_text='Non-secret provider options (domain, region flags, audience ids, etc.).',
    )
    credentials_secret_arn = models.CharField(
        max_length=512,
        blank=True,
        null=True,
        help_text='AWS Secrets Manager ARN; JSON shape depends on provider (e.g. api_key, webhook_signing_key).',
    )
    credentials_secret_region = models.CharField(
        max_length=32,
        blank=True,
        null=True,
        help_text='AWS region for the secret (optional if derivable from ARN)',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'contact_endpoint_email_settings'

    def __str__(self):
        return f'{self.get_provider_display()} for {self.endpoint_id}'

    def clean(self):
        super().clean()
        try:
            validate_email_provider_config(self.provider, self.config or {})
        except ValueError as e:
            raise ValidationError({'config': str(e)}) from e


class ContactEndpointCampaign(models.Model):
    """
    Mapping model to handle many-to-many relationship between ContactEndpoint and Campaign.
    Allows the same contact endpoint to be assigned to multiple campaigns.
    """
    contact_endpoint = models.ForeignKey(
        ContactEndpoint,
        on_delete=models.CASCADE,
        related_name='campaign_mappings',
        help_text="The contact endpoint being mapped to a campaign"
    )
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name='contact_endpoint_mappings',
        help_text="The campaign this contact endpoint is assigned to"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this mapping is currently active"
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'contact_endpoint_campaign'
        unique_together = ('contact_endpoint', 'campaign')
        indexes = [
            models.Index(fields=['contact_endpoint', 'campaign']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        return f"{self.contact_endpoint.value} -> {self.campaign.name}"

class ContactEndpointSmsSettings(models.Model):
    """
    SMS-only settings for a ContactEndpoint.

    This is intentionally separated from ContactEndpoint to keep the core communications
    model channel-agnostic while still providing endpoint-level defaults for inbound SMS.
    """
    endpoint = models.OneToOneField(
        ContactEndpoint,
        on_delete=models.CASCADE,
        related_name='sms_settings',
        help_text='The contact endpoint these SMS settings apply to',
    )

    # Default reply when sender is not opted in to SMS marketing/nurturing contexts.
    not_opted_in_default_reply_message = models.TextField(
        blank=True,
        null=True,
        help_text='Default reply for inbound SMS when sender is not opted in (plain text)',
    )
    not_opted_in_default_reply_template = models.ForeignKey(
        MessageTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='contact_endpoint_not_opted_in_default_sms_settings',
        help_text="ACS SMS template to render as default reply when sender is not opted in",
    )
    # Endpoint-level compliance messages (used when no campaign/rule override is set).
    stop_message = models.TextField(
        blank=True,
        null=True,
        help_text='Message sent when user texts STOP (opt-out confirmation). Used as endpoint default if campaign does not set one.',
    )
    help_message = models.TextField(
        blank=True,
        null=True,
        help_text='Message sent when user texts HELP. Used as endpoint default if campaign/rule do not set one.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'contact_endpoint_sms_settings'

    def __str__(self):
        return f"SMS settings for {self.endpoint.value}"

    def clean(self):
        super().clean()
        if self.not_opted_in_default_reply_template and getattr(self.not_opted_in_default_reply_template, 'channel', None) != 'sms':
            raise ValidationError({
                'not_opted_in_default_reply_template': "Template must have channel='sms'"
            })


class EmailMessage(models.Model):
    """
    Email message log for provider webhooks and outbound correlation.
    Unmanaged mirror of CRM communications.EmailMessage.
    """

    DIRECTION_CHOICES = [
        ('inbound', 'Inbound'),
        ('outbound', 'Outbound'),
        ('event', 'Provider event'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('received', 'Received'),
        ('sent', 'Sent'),
        ('delivered', 'Delivered'),
        ('failed', 'Failed'),
        ('event', 'Event'),
    ]

    endpoint = models.ForeignKey(
        ContactEndpoint,
        on_delete=models.CASCADE,
        related_name='email_messages',
        help_text='Email contact endpoint for this message',
    )
    provider = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text='Email provider (e.g. mailgun, resend)',
    )
    provider_message_id = models.CharField(
        max_length=512,
        blank=True,
        null=True,
        db_index=True,
        help_text='Provider message id (e.g. Mailgun Message-Id or event message id)',
    )
    direction = models.CharField(
        max_length=10,
        choices=DIRECTION_CHOICES,
        default='inbound',
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='received',
    )
    event_type = models.CharField(
        max_length=80,
        blank=True,
        null=True,
        help_text='Provider event name for webhook events (e.g. delivered, failed)',
    )
    from_email = models.CharField(max_length=512, blank=True, null=True)
    to_email = models.CharField(max_length=512, blank=True, null=True)
    subject = models.TextField(blank=True, null=True)
    body_text = models.TextField(blank=True, null=True)
    body_html = models.TextField(blank=True, null=True)
    raw_data = models.JSONField(
        default=dict,
        blank=True,
        help_text='Normalized webhook payload subset or full POST dict',
    )
    webhook_query_params = models.JSONField(
        default=dict,
        blank=True,
        help_text='Query parameters from webhook URL',
    )

    account = models.ForeignKey(
        Account,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='email_messages',
    )
    nurturing_campaign = models.ForeignKey(
        'external_models.LeadNurturingCampaign',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='email_messages',
    )
    in_reply_to_bulk_campaign_message = models.ForeignKey(
        'external_models.BulkCampaignMessage',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='email_inbound_replies',
        help_text='Bulk campaign message this inbound may reply to (future: References header).',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    received_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'communications_email_message'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['endpoint', 'created_at']),
            models.Index(fields=['account', 'created_at']),
            models.Index(fields=['nurturing_campaign', 'created_at']),
            models.Index(fields=['provider', 'provider_message_id']),
            models.Index(fields=['direction', 'status']),
        ]

    def __str__(self):
        return f"{self.direction} {self.from_email} → {self.to_email} ({self.status})"
