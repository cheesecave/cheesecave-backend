"""Email utilities for authentication."""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

from kohakuhub.config import cfg
from kohakuhub.identity import site_name
from kohakuhub.logger import get_logger

logger = get_logger("EMAIL")


def send_verification_email(to_email: str, username: str, token: str) -> bool:
    """Send email verification email.

    Note: HTML emails have limitations:
    - Must use inline CSS (no <style> tags or external stylesheets)
    - Table-based layouts are most reliable (flexbox/grid not supported)
    - Limited CSS properties (many modern CSS features don't work)
    - Different email clients render differently (Outlook, Gmail, Apple Mail, etc.)
    - Must use absolute URLs for images
    - Background images often blocked
    """
    if not cfg.smtp.enabled:
        logger.info(
            f"SMTP disabled. Verification link: {cfg.app.base_url}/api/auth/verify-email?token={token}"
        )
        return True

    name = site_name()
    html_name = escape(name)
    subject = f"Verify your {name} account"
    verify_link = f"{cfg.app.base_url}/api/auth/verify-email?token={token}"

    # Plain text version (fallback)
    text_body = f"""
Hello {username},

Please verify your email address by clicking the link below:

{verify_link}

This link will expire in 24 hours.

If you didn't create this account, please ignore this email.

Best regards,
{name} Team
"""

    # HTML version (styled with table-based layout and inline CSS)
    html_body = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background-color: #f5f5f5;">
    <!-- Main container table -->
    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background-color: #f5f5f5;">
        <tr>
            <td style="padding: 40px 20px;">
                <!-- Content card -->
                <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);">
                    <!-- Header -->
                    <tr>
                        <td style="padding: 40px 40px 20px 40px; text-align: center; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 8px 8px 0 0;">
                            <h1 style="margin: 0; color: #ffffff; font-size: 28px; font-weight: 600;">
                                🎉 Welcome to {html_name}!
                            </h1>
                        </td>
                    </tr>

                    <!-- Body -->
                    <tr>
                        <td style="padding: 40px;">
                            <p style="margin: 0 0 20px 0; font-size: 16px; line-height: 1.6; color: #333333;">
                                Hello <strong>{username}</strong>,
                            </p>

                            <p style="margin: 0 0 30px 0; font-size: 16px; line-height: 1.6; color: #555555;">
                                Thank you for signing up! Please verify your email address to activate your account and start using {html_name}.
                            </p>

                            <!-- CTA Button -->
                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                                <tr>
                                    <td style="text-align: center; padding: 20px 0;">
                                        <a href="{verify_link}" style="display: inline-block; padding: 16px 40px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #ffffff; text-decoration: none; border-radius: 6px; font-size: 16px; font-weight: 600; box-shadow: 0 4px 6px rgba(102, 126, 234, 0.4);">
                                            ✉️ Verify Email Address
                                        </a>
                                    </td>
                                </tr>
                            </table>

                            <p style="margin: 30px 0 20px 0; font-size: 14px; line-height: 1.6; color: #666666;">
                                Or copy and paste this link into your browser:
                            </p>

                            <p style="margin: 0 0 30px 0; padding: 12px; background-color: #f8f9fa; border-left: 4px solid #667eea; border-radius: 4px; font-size: 13px; word-break: break-all; color: #555555;">
                                {verify_link}
                            </p>

                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="border-top: 1px solid #e5e7eb; margin-top: 30px; padding-top: 20px;">
                                <tr>
                                    <td style="padding: 15px; background-color: #fef3c7; border-radius: 6px; border-left: 4px solid #f59e0b;">
                                        <p style="margin: 0; font-size: 14px; color: #92400e; line-height: 1.5;">
                                            ⏰ <strong>Important:</strong> This link will expire in 24 hours.
                                        </p>
                                    </td>
                                </tr>
                            </table>

                            <p style="margin: 30px 0 0 0; font-size: 14px; line-height: 1.6; color: #999999;">
                                If you didn't create this account, you can safely ignore this email.
                            </p>
                        </td>
                    </tr>

                    <!-- Footer -->
                    <tr>
                        <td style="padding: 30px 40px; background-color: #f8f9fa; border-radius: 0 0 8px 8px; text-align: center;">
                            <p style="margin: 0 0 10px 0; font-size: 14px; color: #666666;">
                                Best regards,<br>
                                <strong>The {html_name} Team</strong>
                            </p>
                            <p style="margin: 0; font-size: 12px; color: #999999;">
                                This is an automated email, please do not reply.
                            </p>
                        </td>
                    </tr>
                </table>

                <!-- Disclaimer -->
                <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="max-width: 600px; margin: 20px auto 0 auto;">
                    <tr>
                        <td style="padding: 20px; text-align: center; font-size: 12px; color: #999999; line-height: 1.5;">
                            © 2025 {html_name}. All rights reserved.<br>
                            You received this email because you registered an account.
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
"""

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = cfg.smtp.from_email
        msg["To"] = to_email
        msg["Subject"] = subject

        # Attach both plain text and HTML versions
        # Email clients will choose which to display (preferring HTML if supported)
        part1 = MIMEText(text_body, "plain")
        part2 = MIMEText(html_body, "html")
        msg.attach(part1)
        msg.attach(part2)

        with smtplib.SMTP(cfg.smtp.host, cfg.smtp.port) as server:
            if cfg.smtp.use_tls:
                server.starttls()
            if cfg.smtp.username and cfg.smtp.password:
                server.login(cfg.smtp.username, cfg.smtp.password)
            server.send_message(msg)

        return True
    except Exception as e:
        logger.exception("Failed to send verification email", e)
        return False


def send_org_invitation_email(
    to_email: str, org_name: str, inviter_username: str, token: str, role: str
) -> bool:
    """Send organization invitation email.

    Args:
        to_email: Recipient email address
        org_name: Organization name
        inviter_username: Username of the person sending the invitation
        token: Invitation token
        role: Role being offered (member/admin)

    Returns:
        True if email sent successfully, False otherwise
    """
    if not cfg.smtp.enabled:
        logger.info(
            f"SMTP disabled. Invitation link: {cfg.app.base_url}/invite/{token}"
        )
        return True

    name = site_name()
    html_name = escape(name)
    subject = f"You've been invited to join {org_name} on {name}"
    invite_link = f"{cfg.app.base_url}/invite/{token}"

    # Plain text version (fallback)
    text_body = f"""
Hello,

{inviter_username} has invited you to join the "{org_name}" organization on {name} as a {role}.

Click the link below to accept this invitation:

{invite_link}

This invitation will expire in 7 days.

If you don't have an account yet, you'll need to create one before accepting the invitation.

If you weren't expecting this invitation, you can safely ignore this email.

Best regards,
{name} Team
"""

    # HTML version (styled with table-based layout and inline CSS)
    html_body = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background-color: #f5f5f5;">
    <!-- Main container table -->
    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background-color: #f5f5f5;">
        <tr>
            <td style="padding: 40px 20px;">
                <!-- Content card -->
                <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);">
                    <!-- Header -->
                    <tr>
                        <td style="padding: 40px 40px 20px 40px; text-align: center; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 8px 8px 0 0;">
                            <h1 style="margin: 0; color: #ffffff; font-size: 28px; font-weight: 600;">
                                🎉 Organization Invitation
                            </h1>
                        </td>
                    </tr>

                    <!-- Body -->
                    <tr>
                        <td style="padding: 40px;">
                            <p style="margin: 0 0 20px 0; font-size: 16px; line-height: 1.6; color: #333333;">
                                Hello,
                            </p>

                            <p style="margin: 0 0 30px 0; font-size: 16px; line-height: 1.6; color: #555555;">
                                <strong>{inviter_username}</strong> has invited you to join the <strong>{org_name}</strong> organization on {html_name} as a <strong>{role}</strong>.
                            </p>

                            <!-- Organization Info Box -->
                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background-color: #f8f9fa; border-radius: 6px; margin-bottom: 30px;">
                                <tr>
                                    <td style="padding: 20px;">
                                        <p style="margin: 0 0 10px 0; font-size: 14px; color: #666666;">
                                            <strong>Organization:</strong> {org_name}
                                        </p>
                                        <p style="margin: 0 0 10px 0; font-size: 14px; color: #666666;">
                                            <strong>Role:</strong> {role}
                                        </p>
                                        <p style="margin: 0; font-size: 14px; color: #666666;">
                                            <strong>Invited by:</strong> {inviter_username}
                                        </p>
                                    </td>
                                </tr>
                            </table>

                            <!-- CTA Button -->
                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                                <tr>
                                    <td style="text-align: center; padding: 20px 0;">
                                        <a href="{invite_link}" style="display: inline-block; padding: 16px 40px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #ffffff; text-decoration: none; border-radius: 6px; font-size: 16px; font-weight: 600; box-shadow: 0 4px 6px rgba(102, 126, 234, 0.4);">
                                            ✅ Accept Invitation
                                        </a>
                                    </td>
                                </tr>
                            </table>

                            <p style="margin: 30px 0 20px 0; font-size: 14px; line-height: 1.6; color: #666666;">
                                Or copy and paste this link into your browser:
                            </p>

                            <p style="margin: 0 0 30px 0; padding: 12px; background-color: #f8f9fa; border-left: 4px solid #667eea; border-radius: 4px; font-size: 13px; word-break: break-all; color: #555555;">
                                {invite_link}
                            </p>

                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="border-top: 1px solid #e5e7eb; margin-top: 30px; padding-top: 20px;">
                                <tr>
                                    <td style="padding: 15px; background-color: #fef3c7; border-radius: 6px; border-left: 4px solid #f59e0b;">
                                        <p style="margin: 0; font-size: 14px; color: #92400e; line-height: 1.5;">
                                            ⏰ <strong>Note:</strong> This invitation will expire in 7 days.
                                        </p>
                                    </td>
                                </tr>
                            </table>

                            <p style="margin: 30px 0 0 0; font-size: 14px; line-height: 1.6; color: #999999;">
                                If you don't have an account yet, you'll need to create one before accepting the invitation. If you weren't expecting this invitation, you can safely ignore this email.
                            </p>
                        </td>
                    </tr>

                    <!-- Footer -->
                    <tr>
                        <td style="padding: 30px 40px; background-color: #f8f9fa; border-radius: 0 0 8px 8px; text-align: center;">
                            <p style="margin: 0 0 10px 0; font-size: 14px; color: #666666;">
                                Best regards,<br>
                                <strong>The {html_name} Team</strong>
                            </p>
                            <p style="margin: 0; font-size: 12px; color: #999999;">
                                This is an automated email, please do not reply.
                            </p>
                        </td>
                    </tr>
                </table>

                <!-- Disclaimer -->
                <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="max-width: 600px; margin: 20px auto 0 auto;">
                    <tr>
                        <td style="padding: 20px; text-align: center; font-size: 12px; color: #999999; line-height: 1.5;">
                            © 2025 {html_name}. All rights reserved.<br>
                            You received this email because someone invited you to an organization.
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
"""

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = cfg.smtp.from_email
        msg["To"] = to_email
        msg["Subject"] = subject

        # Attach both plain text and HTML versions
        part1 = MIMEText(text_body, "plain")
        part2 = MIMEText(html_body, "html")
        msg.attach(part1)
        msg.attach(part2)

        with smtplib.SMTP(cfg.smtp.host, cfg.smtp.port) as server:
            if cfg.smtp.use_tls:
                server.starttls()
            if cfg.smtp.username and cfg.smtp.password:
                server.login(cfg.smtp.username, cfg.smtp.password)
            server.send_message(msg)

        logger.success(f"Invitation email sent to {to_email} for org {org_name}")
        return True
    except Exception as e:
        logger.exception("Failed to send invitation email", e)
        return False
