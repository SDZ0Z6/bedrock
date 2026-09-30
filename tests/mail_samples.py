"""测试用的 AWS 邮件样例。

结构照真实邮件来（称呼、字段行、操作步骤、分隔线、页脚、AWS Health 的按钮文字……），
正文措辞是改写过的，账号、密钥、工单号全是假的。账号 111111111111 就是 conftest 台账里的 ALPHA。
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

ACCOUNT = "111111111111"
KEY = "AKIAFAKEKEY000001234"
CASE = "170000000000001"
WHEN = datetime(2026, 9, 27, 7, 13, tzinfo=timezone.utc)


def message(
    sender: str,
    subject: str,
    body: str,
    *,
    name: str = "Amazon Web Services",
    when: datetime = WHEN,
    html: str | None = None,
    auth: str | None = None,
    to: str = "root@example.com",
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{name} <{sender}>" if name else sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = format_datetime(when)
    msg["Message-ID"] = make_msgid(domain="example.com")
    if auth:
        msg["Authentication-Results"] = auth
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


FOOTER = """
===============================================================

Amazon Web Services, Inc. is a subsidiary of Amazon.com, Inc. Amazon.com is a registered trademark of Amazon.com.
This message was produced and distributed by Amazon Web Services, Inc. or its affiliates.
"""

ABUSE = f"""Hello,

As a user of Anthropic models on Amazon Bedrock, you agreed that you and your end users would follow the Anthropic Terms of Service [1].

Anthropic found policy violations through Bedrock's automated detection [2] and has directed AWS to restrict your account's access to Anthropic models.

As a result, in accordance with your agreement with Anthropic, we are taking the following action:

Action: Revoke access to Anthropic models on Bedrock
AWS account ID: {ACCOUNT}
Effective date: 2026-09-27 06:16 (UTC)

If you disagree with this decision, you can appeal directly to the model provider using the appeal method provided [3].
Access will be restored once the model provider tells AWS to restore it.

[1] https://aws.amazon.com/legal/bedrock/third-party-models
[2] https://docs.aws.amazon.com/bedrock/latest/userguide/abuse-detection.html
[3] https://form.example.com/to/appeal

Regards,
AWS Trust & Safety
{FOOTER}
AWS Trust & Safety Center: https://repost.aws/aws-trust-and-safety
"""

SUSPICIOUS = f"""Dear AWS Customer,

Your AWS Account may have been accessed by a third party without permission. Please review the following notice and take immediate action to secure your account.

We saw possibly unwanted activity in your AWS account. It is related to your AWS access key {KEY} belonging to user ReportReader, which suggests that this access key and its secret key are compromised.

To protect your account, we have temporarily limited your ability to use some AWS services.

To restore access, you must contact AWS by 2026-10-02 and follow the instructions below. If you do not contact AWS by 2026-10-02, we may suspend your account. Additionally, we recommend that as a security best practice you enable multi-factor authentication (MFA) on your account.

Follow the instructions below to secure and restore your account.

Step 1:
Replace the exposed key: create a second key, switch your application to it, then make the old key {KEY} inactive.

Step 2: Check your CloudTrail log for unwanted activity.

If you need help, reply through the Support Center: https://console.aws.amazon.com/support/home
{FOOTER}"""

# AWS Health 转发的同一条通知：正文前面多了几行按钮文字，还把主题再写了一遍
HEALTH = f"""AWS Health Event

View in Notification Center

[Action Required] Suspicious activity in your AWS account [{ACCOUNT}]

View details in service console

Hello,

Your AWS Account may have been accessed by a third party without permission. Please review the following notice and take immediate action to secure your account.

We saw possibly unwanted activity in your AWS account. It is related to your AWS access key {KEY} belonging to user ReportReader, which suggests that this access key and its secret key are compromised.

To protect your account, we have temporarily limited your ability to use some AWS services.

To restore access, you must contact AWS by 2026-10-02 and follow the instructions below.

Follow the instructions below to secure and restore your account.
"""

ROOT_REVIEW = """Dear AWS Customer,

We are contacting you because your AWS Account may have been accessed by a third party without permission. Please review this notice and take immediate action to secure and restore your account.

To protect your account, we have temporarily limited your ability to use some AWS services.

To restore access, you must contact AWS by 2026-10-02 and follow the instructions below. If you do not, we may suspend your account.

We ask that you please follow the instructions below to secure and restore your account [1].

Step 1: Change your AWS root account password [2].

Step 2: Enable multi-factor authentication (MFA) on your AWS root user [3].
"""

CASE_NEW = f"""Amazon Web Services has opened case {CASE} on your behalf.

The details of your case are as follows:

Case ID: {CASE}
Account ID: {ACCOUNT}
Severity: urgent

To contact us again about this issue, please use the following link:

https://console.aws.amazon.com/support/home#/case/?displayId={CASE}&language=en

(If you will connect by federation, log in before following the link.)

Sincerely,
The Amazon Web Services Team
{FOOTER}"""

CASE_REPLY = f"""Dear AWS Customer,

We are following up because your AWS Account may still be at risk of being accessed by a third party without permission. This could lead to unexpected charges. If you believe your account is secured, please tell us right away through this support case.

If you have any questions, please contact us by responding through this support case.

Machine translated message

To give updates in your preferred language, machine translation may be used.

===============================================================

To share your experience or contact us again about this case, please return to the AWS Support Center:
https://console.aws.amazon.com/support/home#/case/?displayId={CASE}&language=en
{FOOTER}"""

MFA = """Greetings from Amazon Web Services.

As requested, a multi-factor authentication (MFA) device has been deactivated for the AWS account associated with this email address.

If you did not start this action, you can review your MFA settings on the Security Credentials page in the AWS Management Console.

For additional help, visit the AWS Support Center at https://aws.amazon.com/support

Thank you for using Amazon Web Services.

Sincerely,
The Amazon Web Services Team
"""

VERIFY = """Verify your identity

Hello,

We noticed unusual activity in a recent sign-in attempt from your AWS account's root user root@example.com. If you started this sign-in, enter the following verification code:

482913

If you did not try to sign in, change your password right away.
"""

SUSPENDED = f"""Dear AWS Customer,

Your AWS account {ACCOUNT} has been suspended because of an unpaid balance.

To reactivate it, pay the outstanding balance by 2026-10-05.

Sincerely,
The Amazon Web Services Team
"""

MARKETPLACE = """Dear AWS Marketplace Customer,

You have subscribed to the following product in AWS Marketplace:
* Claude Opus 4.7 (Amazon Bedrock Edition) sold by Anthropic, PBC
"""

OFFER = f"""Greetings from AWS Marketplace,

An AWS Marketplace offer has been accepted by AWS account: {ACCOUNT}.
"""


def abuse(**kw):
    return message(
        "trustandsafety@support.aws.com", f"RE: Your AWS Abuse Report [P01FAKE0000] [AWS ID {ACCOUNT}]",
        ABUSE, name="AWS Trust and Safety", **kw,
    )


def suspicious(**kw):
    return message("no-reply@amazonaws.com", f"[Action Required] Suspicious activity in your AWS account [{ACCOUNT}]", SUSPICIOUS, **kw)


def health(**kw):
    return message("health@aws.com", f"[Action Required] Suspicious activity in your AWS account [{ACCOUNT}]", HEALTH, **kw)


def root_review(**kw):
    return message("no-reply@amazonaws.com", "[Action Required] Please review your AWS Account and credentials", ROOT_REVIEW, **kw)


def case_new(**kw):
    return message("no-reply-aws@amazon.com", f"Amazon Web Services: New Support case: {CASE}", CASE_NEW, **kw)


def case_reply(**kw):
    return message(
        "no-reply-aws@amazon.com",
        f"RE:[CASE {CASE}] [Action Required] Suspicious activity in your AWS account [{ACCOUNT}]",
        CASE_REPLY, **kw,
    )


def mfa(**kw):
    return message("no-reply@amazonaws.com", "Your Amazon Web Services Multi-Factor Authentication (MFA) Has Been Deactivated", MFA, **kw)


def verify(**kw):
    return message("no-reply@signin.aws", "Verify your identity", VERIFY, name="no-reply", **kw)


def suspended(**kw):
    return message("aws-account-notifications@amazon.com", "Your AWS account has been suspended", SUSPENDED, **kw)


def marketplace(**kw):
    return message(
        "no-reply@amazonaws.com", "Your AWS Marketplace subscription for Claude Opus 4.7 (Amazon Bedrock Edition)",
        MARKETPLACE, **kw,
    )


def offer(**kw):
    return message("no-reply@marketplace.aws", "You accepted an AWS Marketplace offer", OFFER, name="AWS Marketplace", **kw)


def invoice(**kw):
    return message("no-reply@amazonaws.com", "Your AWS invoice is available", "Your invoice for September is ready.", **kw)


def stranger(**kw):
    """不是 AWS 发的，主题却装得很像。"""
    return message("alerts@example.com", f"[Action Required] Suspicious activity in your AWS account [{ACCOUNT}]", SUSPICIOUS, name="AWS Security", **kw)


def forged(**kw):
    """发件人写的是 AWS，但收件服务器验出来 DMARC 不通过。"""
    return message(
        "no-reply@amazonaws.com", f"[Action Required] Suspicious activity in your AWS account [{ACCOUNT}]", SUSPICIOUS,
        auth="mx.example.com; spf=fail smtp.mailfrom=evil.example; dkim=none; dmarc=fail header.from=amazonaws.com", **kw,
    )


# 都会发的几封（mfa、verify 是 root 类，发固定群）
ALERTING = (abuse, suspicious, health, root_review, case_new, case_reply, mfa, verify, suspended)
# 都不发的几封
SILENT = (marketplace, offer, invoice, stranger, forged)
