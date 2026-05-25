# cu_reva_ticket_analysis Odoo Module — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `cu_reva_ticket_analysis` Odoo module — a "Analyse with REVA" button on helpdesk tickets and project tasks that submits ticket text to REVA asynchronously, and receives structured HTML back via a FastAPI callback endpoint.

**Architecture:** A shared `reva.ticket.mixin` abstract model adds `reva_status`, `reva_analysis`, and `reva_analysis_id` fields plus the button action to both `helpdesk.ticket` and `project.task`. An OCA FastAPI router at `/api/reva/write-field` handles the inbound REVA callback. Configuration lives in `res.config.settings` backed by `ir.config_parameter`.

**Tech Stack:** Odoo 17+, OCA `fastapi` addon (rest-framework), Pydantic, Python `requests`, Odoo `TransactionCase` / `HttpCase` tests

> **Note:** Replace `<your_test_db>` in all `odoo-bin` commands with your actual Odoo database name.

---

## File Map

| Path | Purpose |
|---|---|
| `odoo/cu_reva_ticket_analysis/__manifest__.py` | Module metadata and dependencies |
| `odoo/cu_reva_ticket_analysis/__init__.py` | Root package init |
| `odoo/cu_reva_ticket_analysis/models/__init__.py` | Models package init |
| `odoo/cu_reva_ticket_analysis/models/reva_mixin.py` | Abstract mixin: fields, `reva_enabled`, `_apply_reva_result`, `action_analyse_reva` |
| `odoo/cu_reva_ticket_analysis/models/helpdesk_ticket.py` | Inherits mixin into `helpdesk.ticket` |
| `odoo/cu_reva_ticket_analysis/models/project_task.py` | Inherits mixin into `project.task` |
| `odoo/cu_reva_ticket_analysis/models/res_config_settings.py` | REVA config: URL, keys, per-model enable flags |
| `odoo/cu_reva_ticket_analysis/models/fastapi_endpoint.py` | Registers OCA FastAPI router |
| `odoo/cu_reva_ticket_analysis/controllers/__init__.py` | Controllers package init |
| `odoo/cu_reva_ticket_analysis/controllers/reva_router.py` | FastAPI router: `POST /api/reva/write-field` |
| `odoo/cu_reva_ticket_analysis/views/helpdesk_ticket_views.xml` | REVA Analysis tab on helpdesk ticket form |
| `odoo/cu_reva_ticket_analysis/views/project_task_views.xml` | REVA Analysis tab on project task form |
| `odoo/cu_reva_ticket_analysis/views/res_config_settings_views.xml` | REVA section in General Settings |
| `odoo/cu_reva_ticket_analysis/security/ir.model.access.csv` | Header-only — concrete models carry their own ACL |
| `odoo/cu_reva_ticket_analysis/data/ir_config_parameter.xml` | Default (empty) config params |
| `odoo/cu_reva_ticket_analysis/data/fastapi_endpoint.xml` | Registers the FastAPI endpoint record |
| `odoo/cu_reva_ticket_analysis/tests/__init__.py` | Tests package init |
| `odoo/cu_reva_ticket_analysis/tests/test_mixin.py` | Field defaults + `_apply_reva_result` |
| `odoo/cu_reva_ticket_analysis/tests/test_action.py` | `action_analyse_reva` with mocked HTTP |
| `odoo/cu_reva_ticket_analysis/tests/test_callback.py` | FastAPI callback via `HttpCase` |

---

### Task 1: Module scaffold

**Files:**
- Create: all `__init__.py`, `__manifest__.py`, and placeholder data/view XMLs

- [ ] **Step 1: Create directories**

```bash
mkdir -p odoo/cu_reva_ticket_analysis/{models,controllers,views,security,data,tests}
```

- [ ] **Step 2: Write `__manifest__.py`**

```python
# odoo/cu_reva_ticket_analysis/__manifest__.py
{
    'name': 'REVA Ticket Analysis',
    'version': '17.0.1.0.0',
    'category': 'Technical',
    'summary': 'AI-powered requirements analysis for Helpdesk tickets and Project tasks',
    'depends': ['helpdesk', 'project', 'fastapi'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_config_parameter.xml',
        'data/fastapi_endpoint.xml',
        'views/res_config_settings_views.xml',
        'views/helpdesk_ticket_views.xml',
        'views/project_task_views.xml',
    ],
    'installable': True,
    'license': 'LGPL-3',
}
```

- [ ] **Step 3: Write `__init__.py`**

```python
# odoo/cu_reva_ticket_analysis/__init__.py
from . import models, controllers
```

- [ ] **Step 4: Write `models/__init__.py`**

```python
# odoo/cu_reva_ticket_analysis/models/__init__.py
from . import reva_mixin
from . import helpdesk_ticket
from . import project_task
from . import res_config_settings
from . import fastapi_endpoint
```

- [ ] **Step 5: Write `controllers/__init__.py`**

```python
# odoo/cu_reva_ticket_analysis/controllers/__init__.py
from . import reva_router
```

- [ ] **Step 6: Write `tests/__init__.py`**

```python
# odoo/cu_reva_ticket_analysis/tests/__init__.py
from . import test_mixin
from . import test_action
from . import test_callback
```

- [ ] **Step 7: Write `security/ir.model.access.csv`**

```csv
id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
```

- [ ] **Step 8: Create placeholder XML files**

Each file below must exist (manifest references them), but will be filled in later tasks.

```xml
<!-- odoo/cu_reva_ticket_analysis/data/ir_config_parameter.xml -->
<?xml version="1.0" encoding="utf-8"?>
<odoo/>
```

```xml
<!-- odoo/cu_reva_ticket_analysis/data/fastapi_endpoint.xml -->
<?xml version="1.0" encoding="utf-8"?>
<odoo/>
```

```xml
<!-- odoo/cu_reva_ticket_analysis/views/res_config_settings_views.xml -->
<?xml version="1.0" encoding="utf-8"?>
<odoo/>
```

```xml
<!-- odoo/cu_reva_ticket_analysis/views/helpdesk_ticket_views.xml -->
<?xml version="1.0" encoding="utf-8"?>
<odoo/>
```

```xml
<!-- odoo/cu_reva_ticket_analysis/views/project_task_views.xml -->
<?xml version="1.0" encoding="utf-8"?>
<odoo/>
```

- [ ] **Step 9: Create empty Python stubs**

```bash
touch odoo/cu_reva_ticket_analysis/models/reva_mixin.py
touch odoo/cu_reva_ticket_analysis/models/helpdesk_ticket.py
touch odoo/cu_reva_ticket_analysis/models/project_task.py
touch odoo/cu_reva_ticket_analysis/models/res_config_settings.py
touch odoo/cu_reva_ticket_analysis/models/fastapi_endpoint.py
touch odoo/cu_reva_ticket_analysis/controllers/reva_router.py
touch odoo/cu_reva_ticket_analysis/tests/test_mixin.py
touch odoo/cu_reva_ticket_analysis/tests/test_action.py
touch odoo/cu_reva_ticket_analysis/tests/test_callback.py
```

- [ ] **Step 10: Commit**

```bash
git add odoo/
git commit -m "feat(odoo): scaffold cu_reva_ticket_analysis module"
```

---

### Task 2: `reva.ticket.mixin` fields + `_apply_reva_result`

**Files:**
- Modify: `odoo/cu_reva_ticket_analysis/models/reva_mixin.py`
- Modify: `odoo/cu_reva_ticket_analysis/models/helpdesk_ticket.py`
- Modify: `odoo/cu_reva_ticket_analysis/models/project_task.py`
- Modify: `odoo/cu_reva_ticket_analysis/tests/test_mixin.py`

- [ ] **Step 1: Write the failing tests**

```python
# odoo/cu_reva_ticket_analysis/tests/test_mixin.py
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'cu_reva')
class TestRevaMixin(TransactionCase):

    def setUp(self):
        super().setUp()
        self.team = self.env['helpdesk.team'].search([], limit=1)
        self.project = self.env['project.project'].search([], limit=1)

    def test_helpdesk_ticket_field_defaults(self):
        ticket = self.env['helpdesk.ticket'].create({
            'name': 'Test Ticket',
            'team_id': self.team.id,
        })
        self.assertEqual(ticket.reva_status, 'draft')
        self.assertFalse(ticket.reva_analysis)
        self.assertEqual(ticket.reva_analysis_id, 0)

    def test_project_task_field_defaults(self):
        task = self.env['project.task'].create({
            'name': 'Test Task',
            'project_id': self.project.id,
        })
        self.assertEqual(task.reva_status, 'draft')
        self.assertFalse(task.reva_analysis)
        self.assertEqual(task.reva_analysis_id, 0)

    def test_apply_reva_result_sets_completed(self):
        ticket = self.env['helpdesk.ticket'].create({
            'name': 'Test Ticket',
            'team_id': self.team.id,
        })
        ticket._apply_reva_result('<h2>Summary</h2><p>Clear ticket.</p>')
        self.assertEqual(ticket.reva_status, 'completed')
        self.assertIn('Summary', ticket.reva_analysis)

    def test_apply_reva_result_is_idempotent(self):
        ticket = self.env['helpdesk.ticket'].create({
            'name': 'Test Ticket',
            'team_id': self.team.id,
        })
        ticket._apply_reva_result('<h2>First</h2>')
        ticket._apply_reva_result('<h2>Second</h2>')
        self.assertIn('Second', ticket.reva_analysis)
        self.assertEqual(ticket.reva_status, 'completed')
```

- [ ] **Step 2: Run to verify failure**

```bash
odoo-bin -d <your_test_db> --test-tags cu_reva --stop-after-init -i cu_reva_ticket_analysis
```
Expected: `AttributeError` — `helpdesk.ticket` has no attribute `reva_status`.

- [ ] **Step 3: Implement `reva_mixin.py`**

```python
# odoo/cu_reva_ticket_analysis/models/reva_mixin.py
from odoo import fields, models


class RevaTicketMixin(models.AbstractModel):
    _name = 'reva.ticket.mixin'
    _description = 'REVA Ticket Analysis Mixin'

    reva_status = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('pending', 'Pending'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
        ],
        default='draft',
        readonly=True,
        string='REVA Status',
    )
    reva_analysis = fields.Html(readonly=True, string='REVA Analysis')
    reva_analysis_id = fields.Integer(default=0, readonly=True, string='REVA Analysis ID')
    reva_enabled = fields.Boolean(compute='_compute_reva_enabled', string='REVA Enabled')

    def _compute_reva_enabled(self):
        ICP = self.env['ir.config_parameter'].sudo()
        key = (
            'reva.helpdesk_enabled'
            if self._name == 'helpdesk.ticket'
            else 'reva.project_enabled'
        )
        enabled = ICP.get_param(key, 'True') not in ('False', '0', '')
        for record in self:
            record.reva_enabled = enabled

    def _apply_reva_result(self, html: str) -> None:
        self.write({'reva_analysis': html, 'reva_status': 'completed'})
```

- [ ] **Step 4: Implement `helpdesk_ticket.py`**

```python
# odoo/cu_reva_ticket_analysis/models/helpdesk_ticket.py
from odoo import models


class HelpdeskTicket(models.Model):
    _name = 'helpdesk.ticket'
    _inherit = ['helpdesk.ticket', 'reva.ticket.mixin']
```

- [ ] **Step 5: Implement `project_task.py`**

```python
# odoo/cu_reva_ticket_analysis/models/project_task.py
from odoo import models


class ProjectTask(models.Model):
    _name = 'project.task'
    _inherit = ['project.task', 'reva.ticket.mixin']
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
odoo-bin -d <your_test_db> --test-tags cu_reva --stop-after-init -u cu_reva_ticket_analysis
```
Expected: 4 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add odoo/cu_reva_ticket_analysis/models/reva_mixin.py \
        odoo/cu_reva_ticket_analysis/models/helpdesk_ticket.py \
        odoo/cu_reva_ticket_analysis/models/project_task.py \
        odoo/cu_reva_ticket_analysis/tests/test_mixin.py
git commit -m "feat(odoo): add reva.ticket.mixin with fields and apply to helpdesk/project"
```

---

### Task 3: Config settings

**Files:**
- Modify: `odoo/cu_reva_ticket_analysis/models/res_config_settings.py`
- Modify: `odoo/cu_reva_ticket_analysis/data/ir_config_parameter.xml`
- Modify: `odoo/cu_reva_ticket_analysis/views/res_config_settings_views.xml`

- [ ] **Step 1: Implement `res_config_settings.py`**

```python
# odoo/cu_reva_ticket_analysis/models/res_config_settings.py
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    reva_url = fields.Char(
        config_parameter='reva.url',
        string='REVA API URL',
        help='Base URL of the REVA service, e.g. https://reva.example.com',
    )
    reva_api_key = fields.Char(
        config_parameter='reva.api_key',
        string='REVA API Key',
        help='Bearer token sent to REVA when submitting tickets (optional)',
    )
    reva_callback_api_key = fields.Char(
        config_parameter='reva.callback_api_key',
        string='REVA Callback API Key',
        help='Bearer token REVA sends on callback — validated by this endpoint',
    )
    reva_helpdesk_enabled = fields.Boolean(
        config_parameter='reva.helpdesk_enabled',
        string='Enable for Helpdesk tickets',
    )
    reva_project_enabled = fields.Boolean(
        config_parameter='reva.project_enabled',
        string='Enable for Project tasks',
    )
```

- [ ] **Step 2: Write `data/ir_config_parameter.xml`**

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo noupdate="1">
    <record id="param_reva_url" model="ir.config_parameter">
        <field name="key">reva.url</field>
        <field name="value"></field>
    </record>
    <record id="param_reva_api_key" model="ir.config_parameter">
        <field name="key">reva.api_key</field>
        <field name="value"></field>
    </record>
    <record id="param_reva_callback_api_key" model="ir.config_parameter">
        <field name="key">reva.callback_api_key</field>
        <field name="value"></field>
    </record>
    <record id="param_reva_helpdesk_enabled" model="ir.config_parameter">
        <field name="key">reva.helpdesk_enabled</field>
        <field name="value">True</field>
    </record>
    <record id="param_reva_project_enabled" model="ir.config_parameter">
        <field name="key">reva.project_enabled</field>
        <field name="value">True</field>
    </record>
</odoo>
```

- [ ] **Step 3: Write `views/res_config_settings_views.xml`**

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="res_config_settings_view_form_reva" model="ir.ui.view">
        <field name="name">res.config.settings.form.reva</field>
        <field name="model">res.config.settings</field>
        <field name="inherit_id" ref="base_setup.action_general_configuration"/>
        <field name="arch" type="xml">
            <div id="integration_settings" position="after">
                <h2>REVA Ticket Analysis</h2>
                <div class="row mt16 o_settings_container">
                    <div class="col-12 col-lg-6 o_setting_box">
                        <div class="o_setting_left_pane"/>
                        <div class="o_setting_right_pane">
                            <label for="reva_url"/>
                            <div class="text-muted">Base URL of the REVA service</div>
                            <div class="content-group">
                                <field name="reva_url" placeholder="https://reva.example.com"/>
                            </div>
                        </div>
                    </div>
                    <div class="col-12 col-lg-6 o_setting_box">
                        <div class="o_setting_left_pane"/>
                        <div class="o_setting_right_pane">
                            <label for="reva_api_key"/>
                            <div class="text-muted">Bearer token sent to REVA (optional)</div>
                            <div class="content-group">
                                <field name="reva_api_key" password="True"/>
                            </div>
                        </div>
                    </div>
                    <div class="col-12 col-lg-6 o_setting_box">
                        <div class="o_setting_left_pane"/>
                        <div class="o_setting_right_pane">
                            <label for="reva_callback_api_key"/>
                            <div class="text-muted">Bearer token REVA sends on callback</div>
                            <div class="content-group">
                                <field name="reva_callback_api_key" password="True"/>
                            </div>
                        </div>
                    </div>
                    <div class="col-12 col-lg-6 o_setting_box">
                        <div class="o_setting_left_pane">
                            <field name="reva_helpdesk_enabled" class="o_light_label"/>
                        </div>
                        <div class="o_setting_right_pane">
                            <label for="reva_helpdesk_enabled"/>
                            <div class="text-muted">Show REVA button on Helpdesk tickets</div>
                        </div>
                    </div>
                    <div class="col-12 col-lg-6 o_setting_box">
                        <div class="o_setting_left_pane">
                            <field name="reva_project_enabled" class="o_light_label"/>
                        </div>
                        <div class="o_setting_right_pane">
                            <label for="reva_project_enabled"/>
                            <div class="text-muted">Show REVA button on Project tasks</div>
                        </div>
                    </div>
                </div>
            </div>
        </field>
    </record>
</odoo>
```

- [ ] **Step 4: Upgrade and verify**

```bash
odoo-bin -d <your_test_db> --stop-after-init -u cu_reva_ticket_analysis
```
Open Settings → General Settings → confirm REVA Ticket Analysis section appears with all five fields.

- [ ] **Step 5: Commit**

```bash
git add odoo/cu_reva_ticket_analysis/models/res_config_settings.py \
        odoo/cu_reva_ticket_analysis/data/ir_config_parameter.xml \
        odoo/cu_reva_ticket_analysis/views/res_config_settings_views.xml
git commit -m "feat(odoo): add REVA config settings"
```

---

### Task 4: `action_analyse_reva` button action

**Files:**
- Modify: `odoo/cu_reva_ticket_analysis/models/reva_mixin.py`
- Modify: `odoo/cu_reva_ticket_analysis/tests/test_action.py`

- [ ] **Step 1: Write the failing tests**

```python
# odoo/cu_reva_ticket_analysis/tests/test_action.py
from unittest.mock import MagicMock, patch
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
import requests as req


@tagged('post_install', '-at_install', 'cu_reva')
class TestActionAnalyseReva(TransactionCase):

    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('reva.url', 'http://reva.test')
        ICP.set_param('reva.api_key', 'test-key')
        self.team = self.env['helpdesk.team'].search([], limit=1)
        self.ticket = self.env['helpdesk.ticket'].create({
            'name': 'Test Ticket',
            'description': '<p>We need a login page.</p>',
            'team_id': self.team.id,
        })

    def _mock_202(self, analysis_id=99):
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_resp.json.return_value = {
            'analysis_id': analysis_id,
            'job_id': 'rq:job:abc',
            'status': 'pending',
        }
        return mock_resp

    def test_action_sets_pending_on_success(self):
        with patch('odoo.addons.cu_reva_ticket_analysis.models.reva_mixin.requests.post', return_value=self._mock_202()):
            self.ticket.action_analyse_reva()
        self.assertEqual(self.ticket.reva_status, 'pending')
        self.assertEqual(self.ticket.reva_analysis_id, 99)

    def test_action_raises_if_url_not_set(self):
        self.env['ir.config_parameter'].sudo().set_param('reva.url', '')
        with self.assertRaises(UserError):
            self.ticket.action_analyse_reva()

    def test_action_raises_on_timeout(self):
        with patch(
            'odoo.addons.cu_reva_ticket_analysis.models.reva_mixin.requests.post',
            side_effect=req.exceptions.Timeout,
        ):
            with self.assertRaises(UserError):
                self.ticket.action_analyse_reva()

    def test_action_raises_on_connection_error(self):
        with patch(
            'odoo.addons.cu_reva_ticket_analysis.models.reva_mixin.requests.post',
            side_effect=req.exceptions.ConnectionError,
        ):
            with self.assertRaises(UserError):
                self.ticket.action_analyse_reva()

    def test_action_raises_on_non_202(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch('odoo.addons.cu_reva_ticket_analysis.models.reva_mixin.requests.post', return_value=mock_resp):
            with self.assertRaises(UserError):
                self.ticket.action_analyse_reva()

    def test_action_posts_correct_payload(self):
        with patch(
            'odoo.addons.cu_reva_ticket_analysis.models.reva_mixin.requests.post',
            return_value=self._mock_202(),
        ) as mock_post:
            self.ticket.action_analyse_reva()

        payload = mock_post.call_args.kwargs['json']
        self.assertEqual(payload['ticket_id'], self.ticket.id)
        self.assertEqual(payload['model_name'], 'helpdesk.ticket')
        self.assertEqual(payload['field_name'], 'reva_analysis')
        self.assertIn('login page', payload['text'])  # HTML stripped to plain text
```

- [ ] **Step 2: Run to verify failure**

```bash
odoo-bin -d <your_test_db> --test-tags cu_reva --stop-after-init -u cu_reva_ticket_analysis
```
Expected: `AttributeError` — `action_analyse_reva` not defined.

- [ ] **Step 3: Replace `reva_mixin.py` with full implementation**

```python
# odoo/cu_reva_ticket_analysis/models/reva_mixin.py
import requests
from odoo import fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext


class RevaTicketMixin(models.AbstractModel):
    _name = 'reva.ticket.mixin'
    _description = 'REVA Ticket Analysis Mixin'

    reva_status = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('pending', 'Pending'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
        ],
        default='draft',
        readonly=True,
        string='REVA Status',
    )
    reva_analysis = fields.Html(readonly=True, string='REVA Analysis')
    reva_analysis_id = fields.Integer(default=0, readonly=True, string='REVA Analysis ID')
    reva_enabled = fields.Boolean(compute='_compute_reva_enabled', string='REVA Enabled')

    def _compute_reva_enabled(self):
        ICP = self.env['ir.config_parameter'].sudo()
        key = (
            'reva.helpdesk_enabled'
            if self._name == 'helpdesk.ticket'
            else 'reva.project_enabled'
        )
        enabled = ICP.get_param(key, 'True') not in ('False', '0', '')
        for record in self:
            record.reva_enabled = enabled

    def _apply_reva_result(self, html: str) -> None:
        self.write({'reva_analysis': html, 'reva_status': 'completed'})

    def action_analyse_reva(self):
        self.ensure_one()
        ICP = self.env['ir.config_parameter'].sudo()
        reva_url = ICP.get_param('reva.url', '')
        reva_api_key = ICP.get_param('reva.api_key', '')

        if not reva_url:
            raise UserError(
                "REVA is not configured. Please set the REVA API URL in Settings → Technical → REVA."
            )

        plain_text = html2plaintext(self.description or '')
        headers = {'Authorization': f'Bearer {reva_api_key}'} if reva_api_key else {}

        try:
            resp = requests.post(
                f"{reva_url.rstrip('/')}/api/v1/ticket-analysis",
                json={
                    'ticket_id': self.id,
                    'model_name': self._name,
                    'field_name': 'reva_analysis',
                    'text': plain_text,
                },
                headers=headers,
                timeout=10,
            )
        except requests.exceptions.Timeout:
            raise UserError("REVA did not respond in time. Please try again in a moment.")
        except requests.exceptions.ConnectionError:
            raise UserError(
                "Could not reach REVA. Check that the REVA API URL is correct and the service is running."
            )

        if resp.status_code != 202:
            raise UserError(
                f"REVA returned an unexpected response ({resp.status_code}). Please contact your administrator."
            )

        data = resp.json()
        self.write({
            'reva_status': 'pending',
            'reva_analysis_id': data['analysis_id'],
        })
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
odoo-bin -d <your_test_db> --test-tags cu_reva --stop-after-init -u cu_reva_ticket_analysis
```
Expected: 4 (mixin) + 6 (action) = 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add odoo/cu_reva_ticket_analysis/models/reva_mixin.py \
        odoo/cu_reva_ticket_analysis/tests/test_action.py
git commit -m "feat(odoo): implement action_analyse_reva with outbound REVA HTTP call"
```

---

### Task 5: FastAPI callback endpoint

**Files:**
- Modify: `odoo/cu_reva_ticket_analysis/controllers/reva_router.py`
- Modify: `odoo/cu_reva_ticket_analysis/models/fastapi_endpoint.py`
- Modify: `odoo/cu_reva_ticket_analysis/data/fastapi_endpoint.xml`
- Modify: `odoo/cu_reva_ticket_analysis/tests/test_callback.py`

- [ ] **Step 1: Write the failing tests**

```python
# odoo/cu_reva_ticket_analysis/tests/test_callback.py
import json
from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install', 'cu_reva')
class TestRevaCallback(HttpCase):

    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('reva.callback_api_key', 'secret-callback-key')
        self.team = self.env['helpdesk.team'].search([], limit=1)
        self.ticket = self.env['helpdesk.ticket'].create({
            'name': 'Callback Test Ticket',
            'team_id': self.team.id,
            'reva_status': 'pending',
            'reva_analysis_id': 42,
        })

    def _post(self, payload, token='secret-callback-key'):
        return self.url_open(
            '/api/reva/write-field',
            data=json.dumps(payload),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {token}',
            },
        )

    def test_callback_writes_html_and_sets_completed(self):
        resp = self._post({
            'ticket_id': self.ticket.id,
            'model_name': 'helpdesk.ticket',
            'field_name': 'reva_analysis',
            'html': '<h2>Summary</h2><p>Clear ticket.</p>',
        })
        self.assertEqual(resp.status_code, 200)
        self.ticket.invalidate_recordset()
        self.assertEqual(self.ticket.reva_status, 'completed')
        self.assertIn('Summary', self.ticket.reva_analysis)

    def test_callback_rejects_wrong_token(self):
        resp = self._post({
            'ticket_id': self.ticket.id,
            'model_name': 'helpdesk.ticket',
            'field_name': 'reva_analysis',
            'html': '<h2>X</h2>',
        }, token='wrong-key')
        self.assertEqual(resp.status_code, 401)

    def test_callback_rejects_unknown_model(self):
        resp = self._post({
            'ticket_id': 1,
            'model_name': 'res.users',
            'field_name': 'reva_analysis',
            'html': '<h2>X</h2>',
        })
        self.assertEqual(resp.status_code, 400)

    def test_callback_returns_404_for_missing_record(self):
        resp = self._post({
            'ticket_id': 999999,
            'model_name': 'helpdesk.ticket',
            'field_name': 'reva_analysis',
            'html': '<h2>X</h2>',
        })
        self.assertEqual(resp.status_code, 404)
```

- [ ] **Step 2: Run to verify failure**

```bash
odoo-bin -d <your_test_db> --test-tags cu_reva --stop-after-init -u cu_reva_ticket_analysis
```
Expected: 404 or connection refused — endpoint not registered yet.

- [ ] **Step 3: Implement `controllers/reva_router.py`**

```python
# odoo/cu_reva_ticket_analysis/controllers/reva_router.py
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from odoo.addons.fastapi.dependencies import odoo_env

router = APIRouter()

_ALLOWED_MODELS = frozenset({'helpdesk.ticket', 'project.task'})


class WriteFieldRequest(BaseModel):
    ticket_id: int
    model_name: str
    field_name: str
    html: str


@router.post('/write-field')
def write_field(
    body: WriteFieldRequest,
    authorization: str = Header(...),
    env=Depends(odoo_env),
):
    ICP = env['ir.config_parameter'].sudo()
    expected_key = ICP.get_param('reva.callback_api_key', '')
    token = authorization.removeprefix('Bearer ').strip()

    if not expected_key or token != expected_key:
        raise HTTPException(status_code=401, detail='Unauthorized')

    if body.model_name not in _ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail=f'Unknown model: {body.model_name}')

    record = env[body.model_name].browse(body.ticket_id)
    if not record.exists():
        raise HTTPException(status_code=404, detail='Record not found')

    record._apply_reva_result(body.html)
    return {'ok': True}
```

- [ ] **Step 4: Implement `models/fastapi_endpoint.py`**

```python
# odoo/cu_reva_ticket_analysis/models/fastapi_endpoint.py
from odoo import fields, models
from ..controllers.reva_router import router as reva_router


class RevaFastApiEndpoint(models.Model):
    _inherit = 'fastapi.endpoint'

    app = fields.Selection(
        selection_add=[('cu_reva', 'REVA Callback')],
        ondelete={'cu_reva': 'cascade'},
    )

    def _get_fastapi_routers(self):
        if self.app == 'cu_reva':
            return [reva_router]
        return super()._get_fastapi_routers()
```

- [ ] **Step 5: Write `data/fastapi_endpoint.xml`**

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="reva_fastapi_endpoint" model="fastapi.endpoint">
        <field name="name">REVA Callback</field>
        <field name="root_path">/api/reva</field>
        <field name="app">cu_reva</field>
    </record>
</odoo>
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
odoo-bin -d <your_test_db> --test-tags cu_reva --stop-after-init -u cu_reva_ticket_analysis
```
Expected: 4 (mixin) + 6 (action) + 4 (callback) = 14 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add odoo/cu_reva_ticket_analysis/controllers/reva_router.py \
        odoo/cu_reva_ticket_analysis/models/fastapi_endpoint.py \
        odoo/cu_reva_ticket_analysis/data/fastapi_endpoint.xml \
        odoo/cu_reva_ticket_analysis/tests/test_callback.py
git commit -m "feat(odoo): add FastAPI callback endpoint POST /api/reva/write-field"
```

---

### Task 6: Helpdesk ticket views

**Files:**
- Modify: `odoo/cu_reva_ticket_analysis/views/helpdesk_ticket_views.xml`

- [ ] **Step 1: Write `views/helpdesk_ticket_views.xml`**

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="helpdesk_ticket_view_form_reva" model="ir.ui.view">
        <field name="name">helpdesk.ticket.form.reva</field>
        <field name="model">helpdesk.ticket</field>
        <field name="inherit_id" ref="helpdesk.helpdesk_ticket_view_form"/>
        <field name="arch" type="xml">
            <notebook position="inside">
                <page string="REVA Analysis" name="reva_analysis">
                    <group>
                        <field name="reva_status"
                               widget="statusbar"
                               statusbar_visible="draft,pending,completed,failed"
                               readonly="1"/>
                    </group>
                    <button name="action_analyse_reva"
                            type="object"
                            string="Analyse with REVA"
                            class="btn-primary mb8"
                            invisible="reva_status not in ('draft', 'failed') or not reva_enabled"/>
                    <field name="reva_analysis"
                           readonly="1"
                           invisible="reva_status != 'completed'"/>
                </page>
            </notebook>
        </field>
    </record>
</odoo>
```

- [ ] **Step 2: Upgrade and verify**

```bash
odoo-bin -d <your_test_db> --stop-after-init -u cu_reva_ticket_analysis
```
Open a helpdesk ticket → confirm "REVA Analysis" tab with status bar and button.

- [ ] **Step 3: Commit**

```bash
git add odoo/cu_reva_ticket_analysis/views/helpdesk_ticket_views.xml
git commit -m "feat(odoo): add REVA Analysis tab to helpdesk ticket form"
```

---

### Task 7: Project task views

**Files:**
- Modify: `odoo/cu_reva_ticket_analysis/views/project_task_views.xml`

- [ ] **Step 1: Write `views/project_task_views.xml`**

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="project_task_view_form_reva" model="ir.ui.view">
        <field name="name">project.task.form.reva</field>
        <field name="model">project.task</field>
        <field name="inherit_id" ref="project.view_task_form2"/>
        <field name="arch" type="xml">
            <notebook position="inside">
                <page string="REVA Analysis" name="reva_analysis">
                    <group>
                        <field name="reva_status"
                               widget="statusbar"
                               statusbar_visible="draft,pending,completed,failed"
                               readonly="1"/>
                    </group>
                    <button name="action_analyse_reva"
                            type="object"
                            string="Analyse with REVA"
                            class="btn-primary mb8"
                            invisible="reva_status not in ('draft', 'failed') or not reva_enabled"/>
                    <field name="reva_analysis"
                           readonly="1"
                           invisible="reva_status != 'completed'"/>
                </page>
            </notebook>
        </field>
    </record>
</odoo>
```

- [ ] **Step 2: Upgrade and verify**

```bash
odoo-bin -d <your_test_db> --stop-after-init -u cu_reva_ticket_analysis
```
Open a project task → confirm "REVA Analysis" tab with status bar and button.

- [ ] **Step 3: Run full test suite one final time**

```bash
odoo-bin -d <your_test_db> --test-tags cu_reva --stop-after-init -u cu_reva_ticket_analysis
```
Expected: 14 tests PASS, 0 failures.

- [ ] **Step 4: Commit**

```bash
git add odoo/cu_reva_ticket_analysis/views/project_task_views.xml
git commit -m "feat(odoo): add REVA Analysis tab to project task form"
```
