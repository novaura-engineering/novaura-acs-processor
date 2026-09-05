import logging
from django.utils import timezone
from external_models.models.nurturing_campaigns import BulkCampaignMessage
from shared_services.email.email_dispatch import effective_email_subject
from enum import Enum

logger = logging.getLogger(__name__)

class ValidationOutcome(Enum):
    """Why validation stopped, so a caller can tell "not yet" from "malformed".

    validate_message_pair returned a bare bool, and returned False both for a message that
    is simply not due yet and for one that can never be sent. The caller marked the whole
    group failed either way -- so a message the send cap had just deferred to the top of
    the next hour came back as a validation failure, and a normal rate limit destroyed it.

    NOT_READY means nothing is wrong: leave the message alone and let a later sweep take
    it when it is due.
    """

    VALID = 'valid'
    NOT_READY = 'not_ready'
    INVALID = 'invalid'


class MessageValidationService:
    """
    Service for validating messages before they are sent.
    Ensures all message components are complete and legal for sending.
    """

    def __init__(self, message_delivery_service):
        self.message_delivery_service = message_delivery_service

    def validate_message_pair(
        self,
        regular_message: BulkCampaignMessage,
        opt_out_message: BulkCampaignMessage = None,
    ) -> 'ValidationOutcome':
        """
        Validates a pair of messages (regular and opt-out) before sending.
        
        Args:
            regular_message: The regular campaign message to validate
            opt_out_message: Optional opt-out message to validate
            
        Returns:
            ValidationOutcome: VALID to send now, NOT_READY to leave for a later sweep,
            INVALID when the message cannot be sent as configured.
        """
        try:
            campaign = regular_message.campaign
            participant = regular_message.participant
            lead = participant.lead

            # Basic campaign and participant validation
            if not campaign.can_send_message(participant):
                logger.warning(f"Participant {participant.id} not eligible for sending")
                return ValidationOutcome.INVALID

            # Validate campaign has proper contact endpoint mappings
            if not self._validate_campaign_contact_endpoints(campaign):
                logger.warning(f"Campaign {campaign.id} has contact endpoint mapping issues")
                return ValidationOutcome.INVALID

            # Validate regular message content
            if campaign.campaign_type == 'reminder':
                if not regular_message.reminder_message:
                    logger.warning(f"Regular message {regular_message.id} has no reminder_message attached")
                    return ValidationOutcome.INVALID
                channel_config = None
                if campaign.channel == 'sms':
                    channel_config = regular_message.reminder_message.sms_config
                elif campaign.channel == 'email':
                    channel_config = regular_message.reminder_message.email_config
                elif campaign.channel == 'voice':
                    channel_config = regular_message.reminder_message.voice_config
                elif campaign.channel == 'chat':
                    channel_config = regular_message.reminder_message.chat_config
                
                # Voice messages use platform_config instead of content
                if campaign.channel == 'voice':
                    if not channel_config or not channel_config.platform_config:
                        logger.warning(f"Regular message {regular_message.id} has no platform_config in reminder_message voice config")
                        return ValidationOutcome.INVALID
                else:
                    if not channel_config or not channel_config.content:
                        logger.warning(f"Regular message {regular_message.id} has no content in reminder_message channel config")
                        return ValidationOutcome.INVALID
            else:
                # For non-reminder campaigns, validate based on channel
                if campaign.channel == 'voice':
                    # Voice messages need platform configuration
                    if not self._validate_voice_platform_config(regular_message):
                        logger.warning(f"Regular message {regular_message.id} has no valid voice platform configuration")
                        return ValidationOutcome.INVALID
                else:
                    # Other channels need content
                    if not regular_message.get_message_content():
                        logger.warning(f"Regular message {regular_message.id} has no content")
                        return ValidationOutcome.INVALID

            # Validate lead contact information
            if campaign.channel in ['sms', 'voice']:
                formatted_number = self.message_delivery_service._format_phone_number(lead.phone_number)
                if not formatted_number:
                    logger.warning(f"Lead {lead.id} has invalid phone number")
                    return ValidationOutcome.INVALID

            # Validate opt-out message if present
            if opt_out_message:
                if campaign.channel == 'voice':
                    # Voice opt-out messages need platform configuration
                    if not self._validate_voice_platform_config(opt_out_message):
                        logger.warning(f"Opt-out message {opt_out_message.id} has no valid voice platform configuration")
                        return ValidationOutcome.INVALID
                else:
                    # Other channels need content
                    if not opt_out_message.get_message_content():
                        logger.warning(f"Opt-out message {opt_out_message.id} has no content")
                        return ValidationOutcome.INVALID

            # Validate message timing
            # A send-cap deferral rewrites scheduled_for to the next reset, so a message
            # can legitimately become not-due between selection and validation. That is a
            # wait, not a defect.
            if regular_message.scheduled_for and regular_message.scheduled_for > timezone.now():
                logger.info(
                    'validation_not_ready bulk_campaign_message_id=%s scheduled_for=%s',
                    regular_message.id,
                    regular_message.scheduled_for,
                )
                return ValidationOutcome.NOT_READY

            if (
                opt_out_message
                and opt_out_message.scheduled_for
                and opt_out_message.scheduled_for > timezone.now()
            ):
                logger.info(
                    'validation_not_ready bulk_campaign_message_id=%s scheduled_for=%s',
                    opt_out_message.id,
                    opt_out_message.scheduled_for,
                )
                return ValidationOutcome.NOT_READY

            # Validate channel-specific requirements
            if not self._validate_channel_requirements(campaign, regular_message, opt_out_message):
                return ValidationOutcome.INVALID

            return ValidationOutcome.VALID

        except Exception as e:
            logger.exception(f"Message validation failed: {e}")
            return ValidationOutcome.INVALID

    def _validate_campaign_contact_endpoints(self, campaign) -> bool:
        """
        Validates that a campaign has proper contact endpoint mappings.
        Ensures the campaign has access to the necessary contact endpoints for sending messages.
        
        Args:
            campaign: The campaign to validate
            
        Returns:
            bool: True if campaign has proper contact endpoint mappings
        """
        try:
            # If no CRM campaign is associated, we can't validate contact endpoints
            if not campaign.crm_campaign:
                logger.debug(f"Campaign {campaign.id} has no CRM campaign associated, skipping contact endpoint validation")
                return True
            
            from external_models.models.communications import ContactEndpoint, ContactEndpointCampaign
            
            # Check if there are any active contact endpoints mapped to this campaign
            # Use the ContactEndpointCampaign mapping table instead of direct foreign key
            contact_endpoint_mappings = ContactEndpointCampaign.objects.filter(
                campaign=campaign.crm_campaign,
                is_active=True,
                contact_endpoint__channels__channel=campaign.channel
            ).select_related('contact_endpoint').distinct()
            
            if not contact_endpoint_mappings.exists():
                logger.warning(f"Campaign {campaign.id} has no active contact endpoints mapped for channel {campaign.channel}")
                return False
            
            # Check if the campaign's channel configs have valid from endpoints
            if campaign.channel == 'sms' and campaign.sms_config:
                if not campaign.sms_config.from_endpoint:
                    logger.warning(f"Campaign {campaign.id} SMS config has no from_endpoint specified")
                    return False
            elif campaign.channel == 'email' and campaign.email_config:
                if not campaign.email_config.from_endpoint:
                    logger.warning(f"Campaign {campaign.id} email config has no from_endpoint specified")
                    return False
            elif campaign.channel == 'voice' and campaign.voice_config:
                if not campaign.voice_config.from_endpoint:
                    logger.warning(f"Campaign {campaign.id} voice config has no from_endpoint specified")
                    return False
            elif campaign.channel == 'chat' and campaign.chat_config:
                if not campaign.chat_config.from_endpoint:
                    logger.warning(f"Campaign {campaign.id} chat config has no from_endpoint specified")
                    return False
            
            return True
            
        except Exception as e:
            logger.exception(f"Contact endpoint validation failed: {e}")
            return False

    def _validate_channel_requirements(self, campaign, regular_message, opt_out_message=None) -> bool:
        """
        Validates channel-specific requirements for messages.
        
        Args:
            campaign: The campaign the messages belong to
            regular_message: The regular message to validate
            opt_out_message: Optional opt-out message to validate
            
        Returns:
            bool: True if all channel-specific requirements are met
        """
        try:
            if campaign.channel == 'sms':
                # Validate SMS-specific requirements
                if not self._validate_sms_requirements(campaign, regular_message, opt_out_message):
                    return False

            elif campaign.channel == 'voice':
                # Validate voice-specific requirements
                if not self._validate_voice_requirements(campaign, regular_message, opt_out_message):
                    return False

            elif campaign.channel == 'email':
                # Validate email-specific requirements
                if not self._validate_email_requirements(campaign, regular_message, opt_out_message):
                    return False

            return True

        except Exception as e:
            logger.exception(f"Channel validation failed: {e}")
            return False

    def _validate_sms_requirements(self, campaign, regular_message, opt_out_message=None) -> bool:
        """Validates SMS-specific requirements"""
        try:
            # Check for valid service phone number
            service_phone = None
            if regular_message.message_type == 'opt_out_notice':
                if campaign.sms_config:
                    service_phone = campaign.sms_config.get_from_number()
            elif regular_message.message_type == 'opt_out_confirmation':
                if campaign.sms_config:
                    service_phone = campaign.sms_config.get_from_number()
            elif campaign.campaign_type == 'drip' and regular_message.drip_message_step:
                service_phone = regular_message.drip_message_step.sms_config.get_from_number()
            elif campaign.campaign_type == 'reminder' and regular_message.reminder_message:
                service_phone = regular_message.reminder_message.sms_config.get_from_number()
            else:
                service_phone = campaign.sms_config.get_from_number()

            if not service_phone:
                logger.warning(f"No valid service phone number found for message {regular_message.id}")
                return False

            # Validate that the contact endpoint is properly mapped to the campaign
            if not self._validate_contact_endpoint_mapping(campaign, 'sms', service_phone):
                logger.warning(f"Service phone number {service_phone} is not properly mapped to campaign {campaign.id}")
                return False

            return True

        except Exception as e:
            logger.exception(f"SMS validation failed: {e}")
            return False

    def _validate_voice_requirements(self, campaign, regular_message, opt_out_message=None) -> bool:
        """Validates voice-specific requirements"""
        try:
            # Check for valid service phone number
            service_phone = None
            if regular_message.message_type == 'opt_out_notice':
                if campaign.voice_config:
                    service_phone = campaign.voice_config.get_from_number()
            elif regular_message.message_type == 'opt_out_confirmation':
                if campaign.voice_config:
                    service_phone = campaign.voice_config.get_from_number()
            elif campaign.campaign_type == 'drip' and regular_message.drip_message_step:
                service_phone = regular_message.drip_message_step.voice_config.get_from_number()
            elif campaign.campaign_type == 'reminder' and regular_message.reminder_message:
                service_phone = regular_message.reminder_message.voice_config.get_from_number()
            else:
                service_phone = campaign.voice_config.get_from_number()

            if not service_phone:
                logger.warning(f"No valid service phone number found for message {regular_message.id}")
                return False

            # Validate that the contact endpoint is properly mapped to the campaign
            if not self._validate_contact_endpoint_mapping(campaign, 'voice', service_phone):
                logger.warning(f"Service phone number {service_phone} is not properly mapped to campaign {campaign.id}")
                return False

            # Validate voice platform configuration
            if not self._validate_voice_platform_config(regular_message):
                logger.warning(f"No valid voice platform configuration found for message {regular_message.id}")
                return False

            return True

        except Exception as e:
            logger.exception(f"Voice validation failed: {e}")
            return False

    def _resolve_email_config_for_validation(self, campaign, regular_message):
        """EmailConfig used for from-address and subject (matches bulk send resolution)."""
        if regular_message.message_type in ('opt_out_notice', 'opt_out_confirmation'):
            return campaign.email_config
        return regular_message.get_effective_email_config()

    def _validate_email_requirements(self, campaign, regular_message, opt_out_message=None) -> bool:
        """Validates email-specific requirements"""
        try:
            email_config = self._resolve_email_config_for_validation(campaign, regular_message)
            if not email_config:
                logger.warning(
                    f"No email_config found for message {regular_message.id} (campaign {campaign.id})"
                )
                return False

            from_address = email_config.get_from_email()
            if not from_address:
                logger.warning(f"No valid from address found for message {regular_message.id}")
                return False

            # Validate that the contact endpoint is properly mapped to the campaign
            if not self._validate_contact_endpoint_mapping(campaign, 'email', from_address):
                logger.warning(f"From address {from_address} is not properly mapped to campaign {campaign.id}")
                return False

            # Subject: optional campaign.subject (CRM may add it), else EmailConfig / hosted template — mirrors send path
            subject = (getattr(campaign, 'subject', None) or '').strip() or effective_email_subject(
                email_config
            )
            if not subject:
                logger.warning(f"No subject found for message {regular_message.id}")
                return False

            return True

        except Exception as e:
            logger.exception(f"Email validation failed: {e}")
            return False

    def _validate_contact_endpoint_mapping(self, campaign, channel, endpoint_value) -> bool:
        """
        Validates that a specific contact endpoint is properly mapped to the campaign.
        
        Args:
            campaign: The campaign to validate
            channel: The communication channel
            endpoint_value: The contact endpoint value (phone number, email, etc.)
            
        Returns:
            bool: True if the contact endpoint is properly mapped
        """
        try:
            # If no CRM campaign is associated, we can't validate mappings
            if not campaign.crm_campaign:
                return True
            
            from external_models.models.communications import ContactEndpoint, ContactEndpointCampaign
            
            # Check if the contact endpoint exists and is mapped to this campaign
            # Use the ContactEndpointCampaign mapping table instead of direct foreign key
            contact_endpoint_mapping = ContactEndpointCampaign.objects.filter(
                campaign=campaign.crm_campaign,
                is_active=True,
                contact_endpoint__value=endpoint_value,
                contact_endpoint__channels__channel=channel
            ).select_related('contact_endpoint').first()
            
            if not contact_endpoint_mapping:
                logger.warning(f"Contact endpoint {endpoint_value} for channel {channel} is not mapped to campaign {campaign.crm_campaign.id}")
                return False
            
            # Check if the contact endpoint is verified (optional but recommended)
            if not contact_endpoint_mapping.contact_endpoint.is_verified:
                logger.warning(f"Contact endpoint {endpoint_value} is not verified")
                # You can choose to return False here if you want to require verification
                # return False
            
            return True
            
        except Exception as e:
            logger.exception(f"Contact endpoint mapping validation failed: {e}")
            return False

    def _validate_voice_platform_config(self, message) -> bool:
        """
        Validates that a voice message has proper platform configuration.
        
        Args:
            message: The message to validate
            
        Returns:
            bool: True if the voice platform configuration is valid
        """
        try:
            campaign = message.campaign
            
            # Get the appropriate voice config based on campaign type and message type
            voice_config = None
            
            if message.message_type in ['opt_out_notice', 'opt_out_confirmation']:
                voice_config = campaign.voice_config
            elif campaign.campaign_type == 'drip' and message.drip_message_step:
                voice_config = message.drip_message_step.voice_config
            elif campaign.campaign_type == 'reminder' and message.reminder_message:
                voice_config = message.reminder_message.voice_config
            else:
                voice_config = campaign.voice_config
            
            if not voice_config:
                logger.warning(f"No voice config found for message {message.id}")
                return False
            
            # Check that platform is specified
            if not voice_config.platform:
                logger.warning(f"No platform specified in voice config for message {message.id}")
                return False
            
            # Check that platform_config exists and is not empty
            if not voice_config.platform_config:
                logger.warning(f"No platform_config found in voice config for message {message.id}")
                return False
            
            # Validate platform-specific configuration based on the platform
            if voice_config.platform == 'bland_ai':
                return self._validate_bland_ai_config(voice_config)
            elif voice_config.platform == 'vapi':
                return self._validate_vapi_config(voice_config)
            elif voice_config.platform == 'elevenlabs':
                return self._validate_elevenlabs_config(voice_config)
            elif voice_config.platform == 'twilio':
                return self._validate_twilio_voice_config(voice_config)
            else:
                logger.warning(f"Unsupported voice platform: {voice_config.platform}")
                return False
                
        except Exception as e:
            logger.exception(f"Voice platform config validation failed: {e}")
            return False

    def _validate_bland_ai_config(self, voice_config) -> bool:
        """Validates Bland AI specific configuration"""
        try:
            config = voice_config.platform_config
            
            # Check for required Bland AI fields
            required_fields = ['pathway_id']  # Bland AI uses 'pathway_id' instead of 'content'
            
            for field in required_fields:
                if not config.get(field):
                    logger.warning(f"Missing required Bland AI field: {field}")
                    return False
            
            # Validate voice ID if specified
            if voice_config.voice_id and not isinstance(voice_config.voice_id, str):
                logger.warning("Voice ID must be a string")
                return False
            
            # Validate max_duration if specified
            if voice_config.max_duration and not isinstance(voice_config.max_duration, int):
                logger.warning("Max duration must be an integer")
                return False
            
            return True
            
        except Exception as e:
            logger.exception(f"Bland AI config validation failed: {e}")
            return False

    def _validate_vapi_config(self, voice_config) -> bool:
        """Validates VAPI specific configuration"""
        try:
            config = voice_config.platform_config
            
            # Check for required VAPI fields
            if 'assistant' not in config:
                logger.warning("VAPI config missing 'assistant' configuration")
                return False
            
            assistant = config['assistant']
            if 'model' not in assistant:
                logger.warning("VAPI assistant config missing 'model'")
                return False
            
            # Validate model
            valid_models = ['gpt-4', 'gpt-3.5-turbo']
            if assistant['model'] not in valid_models:
                logger.warning(f"Invalid VAPI model: {assistant['model']}")
                return False
            
            return True
            
        except Exception as e:
            logger.exception(f"VAPI config validation failed: {e}")
            return False

    def _validate_elevenlabs_config(self, voice_config) -> bool:
        """Validates ElevenLabs specific configuration"""
        try:
            config = voice_config.platform_config
            
            # Check for required ElevenLabs fields
            if 'voice_id' not in config:
                logger.warning("ElevenLabs config missing 'voice_id'")
                return False
            
            # Validate voice_settings if present
            if 'voice_settings' in config:
                voice_settings = config['voice_settings']
                if 'stability' in voice_settings:
                    stability = voice_settings['stability']
                    if not isinstance(stability, (int, float)) or not 0 <= stability <= 1:
                        logger.warning("ElevenLabs stability must be a number between 0 and 1")
                        return False
            
            return True
            
        except Exception as e:
            logger.exception(f"ElevenLabs config validation failed: {e}")
            return False

    def _validate_twilio_voice_config(self, voice_config) -> bool:
        """Validates Twilio Voice specific configuration"""
        try:
            config = voice_config.platform_config
            
            # Check for required Twilio Voice fields
            if 'twiml' not in config and 'url' not in config:
                logger.warning("Twilio Voice config missing 'twiml' or 'url'")
                return False
            
            return True
            
        except Exception as e:
            logger.exception(f"Twilio Voice config validation failed: {e}")
            return False 