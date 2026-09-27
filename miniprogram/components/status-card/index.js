Component({
  properties: {
    state: { type: String, value: 'empty' },
    title: { type: String, value: '' },
    detail: { type: String, value: '' },
    actionLabel: { type: String, value: '' },
  },
  methods: {
    onAction() {
      this.triggerEvent('action');
    },
  },
});
