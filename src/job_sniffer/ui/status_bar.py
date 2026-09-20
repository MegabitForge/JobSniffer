import flet as ft


class StatusBar:
    """Persistent status strip at the bottom of the shell.

    Shows the scan status on the left and the background AI evaluation
    status (with a progress ring) on the right, regardless of the active tab.
    """

    def __init__(self, page: ft.Page) -> None:
        self._page = page

        self.scan_text = ft.Text("", size=14, expand=True)
        self.ai_status_text = ft.Text(
            "", size=13, weight=ft.FontWeight.W_500, color=ft.Colors.BLUE_800
        )
        self.ai_progress_ring = ft.ProgressRing(width=16, height=16, stroke_width=2)
        self.ai_status_row = ft.Row(
            [self.ai_progress_ring, self.ai_status_text], visible=False, spacing=8
        )

        self.control = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Divider(),
                    ft.Row(
                        controls=[self.scan_text, self.ai_status_row],
                        spacing=16,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=6,
            ),
            padding=ft.Padding(4, 0, 4, 4),
        )

    def set_scan(self, message: str) -> None:
        """Update the scan status message."""
        self.scan_text.value = message
        self._page.update()

    def show_ai(self, message: str | None = None) -> None:
        """Show the AI status row with a spinner, optionally setting the message."""
        if message is not None:
            self.ai_status_text.value = message
        self.ai_progress_ring.visible = True
        self.ai_status_row.visible = True
        self._page.update()

    def set_ai(self, message: str) -> None:
        """Update the AI status message without changing visibility."""
        self.ai_status_text.value = message
        self._page.update()

    def finish_ai(self, message: str) -> None:
        """Update the AI status message and hide the progress ring."""
        self.ai_status_text.value = message
        self.ai_progress_ring.visible = False
        self._page.update()

    def hide_ai(self) -> None:
        """Hide the AI status row and restore the spinner for next use."""
        self.ai_status_row.visible = False
        self.ai_progress_ring.visible = True
        self._page.update()
