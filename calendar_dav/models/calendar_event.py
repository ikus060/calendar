# Copyright 2026 IKUS Software <patrik@ikus-soft.com>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

from odoo import api, fields, models

class Meeting(models.Model):
    """
    Extend calendar.event with CalDAV mixin.
    Adds dav_uid and etag support via dav.mixin.
    """
    _name = "calendar.event"
    _inherit = ["calendar.event", "dav.mixin"]

    is_base_event = fields.Boolean(
        string='Is Base Event',
        compute='_compute_is_base_event',
        store=True
    )

    @api.depends('recurrency', 'recurrence_id', 'recurrence_id.base_event_id', 'recurrence_id.calendar_event_ids')
    def _compute_is_base_event(self):
        """
        Return True if this event is a base event.
        """
        for record in self:
            record.is_base_event = not record.recurrence_id or record.recurrence_id.base_event_id == record

    def unlink(self):
        """
        Handle deletion of all the recurrence.
        """
        # TODO I'm not sure it's the best place to implement this logic.
        # I have the feeling it should be in the mapper, but then all
        # deletion would go trought the mapper.
        if self._context.get('dav_delete'):
            recurrences = self.mapped('recurrence_id')
            all_events = self | recurrences.mapped('calendar_event_ids')
            res = all_events.with_context(dav_delete=False).unlink()
            recurrences.exists().unlink()
            return res
        return super().unlink()