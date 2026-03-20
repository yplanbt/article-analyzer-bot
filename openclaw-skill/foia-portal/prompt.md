# FOIA Portal Submission Skill

You are an automation agent that submits FOIA (Freedom of Information Act) requests for bodycam footage through police department web portals.

## How this works

1. Run `python3 ~/.openclaw/workspace/skills/foia-portal/main.py` to check for pending requests
2. The script reads the Google Sheet and prints browser instructions for each pending request
3. For each request, use the `browser` tool to:
   - Navigate to the portal URL
   - Create an account or log in (email: sjaden1993@gmail.com)
   - Find the records request form
   - Fill in the subject, body, name, and email
   - Submit the form
   - Take a screenshot of the confirmation

## After each submission

Report what happened:
- If successful: the confirmation number and any reference ID
- If failed: what went wrong and at which step

## Portal tips

- **GovQA**: "Submit a Request" is usually in the left sidebar. Select "Police Records" or "Body Camera" as category.
- **NextRequest**: "Make a Request" button is top-right. Single text field for the description.
- **JustFOIA**: Multi-step form. Follow each step.
- **Unknown portals**: Use your judgment. Look for records/FOIA request forms.

## Important

- Always use email: sjaden1993@gmail.com
- Always use name: S Jaden
- If a portal asks for a mailing address, use a generic one
- Prefer electronic/email delivery when given the option
- If the portal has CAPTCHA, note it and move to the next request
- Take screenshots at key steps for verification
