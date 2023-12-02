# Copyright 2023 James Teh
# License: GNU General Public License

import base64
import io
import json
import os
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import wx

import api
import config
import globalPluginHandler
import gui
import queueHandler
import screenBitmap
import speech
import ui
from logHandler import log
from scriptHandler import script

DEFAULT_URL = "http://localhost:8080/"
PROMPT = "This is a conversation between User and Llama, a friendly chatbot. Llama is helpful, kind, honest, good at writing, and never fails to answer any requests immediately and with precision. Llama is especially good at describing images in great detail for users who can't see.\nUSER: [img-10] Please describe this image in detail.\nASSISTANT:"

class GlobalPlugin(globalPluginHandler.GlobalPlugin):

	@script(
		gesture="kb:NVDA+shift+l",
		description="Recognizes the content of the current navigator object with llama.cpp."
	)
	def script_recognizeWithLlamaCpp(self, gesture):
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
		imgData = base64.b64encode(stream.getvalue())
		if (
			(confSect := config.conf.get("llamaCpp")) and
			(confUrl := confSect.get("url"))
		):
			self._url = confUrl
		else:
			self._url = DEFAULT_URL
		self._url = urllib.parse.urljoin(self._url, "completion")
		self._imgData = imgData.decode("UTF-8")
		ui.message("Recognizing")
		# Maintain a history of the conversation, as we have to re-send this with
		# each subsequent query.
		self._history = PROMPT
		# Create the dialog so the background thread can reference methods on it, but
		# don't show it yet. The dialog will show itself when the first token of the
		# response is provided.
		self._dialog = ResultDialog(self)
		self._query()

	def _query(self):
		# The web request is synchronous, so run it in a thread.
		self._thread = threading.Thread(target=self._bgQuery)
		self._thread.start()

	def _bgQuery(self):
		try:
			req = urllib.request.Request(
				self._url,
				data=json.dumps({
					"prompt": self._history,
					"stream": True,
					"image_data": [
						{"id": 10, "data": self._imgData}
					],
				}).encode(),
				headers={"Content-Type": "application/json"}
			)
			resp = urllib.request.urlopen(req)
			for token in resp:
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

	def _send(self, query):
		"""Called by the dialog when the user sends a follow-up query.
		"""
		self._history += "\nUSER: %s\nASSISTANT:" % query
		ui.message("Please wait")
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
