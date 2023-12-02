# Copyright 2023 James Teh
# License: GNU General Public License

import base64
import http.client
import io
import json
import os
import sys
import threading
import wx

import addonHandler
import api
import config
import globalPluginHandler
import gui
from gui import guiHelper, nvdaControls
from gui.settingsDialogs import SettingsPanel
import queueHandler
import screenBitmap
import speech
import ui
from logHandler import log
from scriptHandler import script

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8080
PROMPT = "This is a conversation between User and Llama, a friendly chatbot. Llama is helpful, kind, honest, good at writing, and never fails to answer any requests immediately and with precision. Llama is especially good at describing images in great detail for users who can't see.\nUSER: [img-10] Please describe this image in detail.\nASSISTANT:"
# This can take a long time, particularly if running on the CPU.
DEFAULT_TIMEOUT = 180

_curAddon = addonHandler.getCodeAddon()
addonName = _curAddon.name.lower()
_addonSummary = _curAddon.manifest['summary']
addonHandler.initTranslation()

confspec = {
	"host": f"string(default={DEFAULT_HOST})",
	"port": f"integer(default={DEFAULT_PORT})",
	"timeout": f"integer(default={DEFAULT_TIMEOUT})"
}
config.conf.spec[addonName] = confspec


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	scriptCategory = _addonSummary

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.config = config.conf[addonName]
		LlamaCppSettingsPanel.config = self.config
		gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(LlamaCppSettingsPanel)

	def terminate(self):
		gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(LlamaCppSettingsPanel)

	@script(
		gesture="kb:NVDA+shift+l",
		description=_("Recognizes the content of the current navigator object with llama.cpp.")
	)
	def script_recognizeWithLlamaCpp(self, gesture):
		self._imgData = self.takeScreenshot()
		ui.message(_("Recognizing"))
		# Maintain a history of the conversation, as we have to re-send this with
		# each subsequent query.
		self._history = PROMPT
		# Create the dialog so the background thread can reference methods on it, but
		# don't show it yet. The dialog will show itself when the first token of the
		# response is provided.
		self._dialog = ResultDialog(self)
		self._query()

	def takeScreenshot(self):
		location = api.getNavigatorObject().location
		sb = screenBitmap.ScreenBitmap(location.width, location.height)
		pixels = sb.captureImage(
			location.left, location.top, location.width, location.height
		)
		img = wx.EmptyBitmap(location.width, location.height, 32)
		img.CopyFromBuffer(pixels, wx.BitmapBufferFormat_RGB32)
		img = img.ConvertToImage()
		stream = io.BytesIO()
		img.SaveFile(stream, wx.BITMAP_TYPE_JPEG)
		imgData = base64.b64encode(stream.getvalue()).decode("UTF-8")
		return imgData

	def _query(self):
		# The web request is synchronous, so run it in a thread.
		self._thread = threading.Thread(target=self._bgQuery)
		self._thread.start()

	def _bgQuery(self):
		try:
			host = self.config["host"]
			port = self.config["port"]
			timeout = self.config["timeout"]
			connection = http.client.HTTPConnection(host, port, timeout=timeout)
			data = json.dumps({
				"prompt": self._history,
				"stream": True,
				"image_data": [
					{"id": 10, "data": self._imgData}
				],
			})
			headers = {"Content-Type": "application/json"}
			connection.request("POST", "/completion", body=data, headers=headers)
			response = connection.getresponse()
			for token in response:
				if self._thread is not threading.current_thread():
					# This previous request was cancelled.
					return
				token = token.rstrip()
				if not token:
					continue
				# Strip "data: " prefix.
				token = token[6:]
				token = json.loads(token)
				content = token["content"]
				if not content:
					continue
				self._history += content
				wx.CallAfter(self._dialog.addResponse, content)
			wx.CallAfter(self._dialog.responseDone)
		except Exception:
			log.exception("")
		finally:
			connection.close()

	def _send(self, query):
		"""Called by the dialog when the user sends a follow-up query.
		"""
		self._history += "\nUSER: %s\nASSISTANT:" % query
		ui.message(_("Please wait"))
		self._query()

	def _finish(self):
		"""Called when the user closes the dialog and is thus finished with the session.
		"""
		self._dialog = None
		# If the thread is running, it will exit early when it wakes and sees that
		# self._thread is no longer the same thread.
		self._thread = None

class ResultDialog(wx.Dialog):

	def __init__(self, plugin):
		super().__init__(gui.mainFrame, title="Llama Chat")
		self.plugin = plugin
		self.Bind(wx.EVT_CLOSE, self.onClose)
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		self.outputCtrl = wx.TextCtrl(
			self,
			size=(1500, 1000),
			style=wx.TE_MULTILINE | wx.TE_READONLY
		)
		mainSizer.Add(self.outputCtrl, proportion=2, flag=wx.EXPAND)
		inputSizer = wx.BoxSizer(wx.HORIZONTAL)
		inputLabel = wx.StaticText(self, label="Ask a question")
		inputSizer.Add(inputLabel)
		self.inputCtrl = wx.TextCtrl(self)
		inputSizer.Add(self.inputCtrl)
		self.sendButton = wx.Button(self, label="Send")
		self.sendButton.SetDefault()
		self.sendButton.Disable()
		self.sendButton.Bind(wx.EVT_BUTTON, self.onSend)
		inputSizer.Add(self.sendButton)
		mainSizer.Add(inputSizer)
		self.SetSizer(mainSizer)
		mainSizer.Fit(self)
		self.isResponseStreaming = False
		self.speechBuffer = ""

	def addResponse(self, text):
		# Add text as it comes in, but keep the user's cursor where it is.
		pos = self.outputCtrl.InsertionPoint
		if not self.isResponseStreaming:
			self.outputCtrl.AppendText("Llama: ")
			self.isResponseStreaming = True
		self.outputCtrl.AppendText(text)
		self.outputCtrl.InsertionPoint = pos
		self.speechBuffer += text
		if not self.Shown:
			self.Raise()
			self.Show()
			self.outputCtrl.SetFocus()
		# We don't speak every token individually, as that is very jarring. However,
		# we don't want to wait until the very end either. If there are 10 or more
		# tokens, speak them.
		if len(self.speechBuffer.split(" ")) >= 10:
			speech.speakMessage(self.speechBuffer)
			self.speechBuffer = ""

	def responseDone(self):
		self.isResponseStreaming = False
		# Speak the rest of the response.
		speech.speakMessage(self.speechBuffer)
		self.speechBuffer = ""
		self.sendButton.Enable()

	def onSend(self, event):
		if not self.sendButton.Enabled:
			return
		self.sendButton.Disable()
		self.plugin._send(self.inputCtrl.Value)
		self.outputCtrl.AppendText("\nUser: %s\n" % self.inputCtrl.Value)
		self.inputCtrl.Clear()

	def onClose(self, event):
		self.plugin._finish()
		self.plugin = None
		self.Destroy()


class LlamaCppSettingsPanel(SettingsPanel):
    # Translators: name of the dialog.
	title = "LlamaCpp"

	def makeSettings(self, sizer):
		sHelper = gui.guiHelper.BoxSizerHelper(self, sizer=sizer)
		# Translators: A setting in addon settings dialog.
		hostLb = _("&Host address")
		self.hostEdit = sHelper.addLabeledControl(hostLb, wx.TextCtrl)
		self.hostEdit.Value=self.config["host"]
        # Translators: A setting in addon settings dialog.
		portLb = _("&Port")
		self.portSpin = sHelper.addLabeledControl(
			portLb,
			nvdaControls.SelectOnFocusSpinCtrl,
			min=0,
			max=65353,
			initial=self.config["port"])
        # Translators: A setting in addon settings dialog, shown if source language is on auto.
		timeoutLb = _("&Timeout")
		self.timeoutSpin = sHelper.addLabeledControl(
			timeoutLb,
			nvdaControls.SelectOnFocusSpinCtrl,
			min=10,
			max=3600,
			initial=self.config["timeout"])

	def postInit(self):
		self.hostEdit.SetFocus()

	def onSave(self):
		self.config["host"] = self.hostEdit.Value
		self.config["port"] = self.portSpin.Value
		self.config["timeout"] = self.timeoutSpin.Value
