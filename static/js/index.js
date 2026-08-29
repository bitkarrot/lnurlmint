const LNURLMINT_TEMPLATE = `
  <div class="row q-col-gutter-md">
    <div class="col-12">
      <q-card>
        <q-card-section>
          <div class="row items-center q-mb-md">
            <div class="col">
              <h5 class="text-subtitle1 q-my-none">lnurlmint</h5>
            </div>
            <div class="col-auto">
              <q-btn
                unelevated
                color="primary"
                label="Create Mint"
                @click="openCreateDialog"
              ></q-btn>
            </div>
          </div>

          <q-banner v-if="errorMessage" class="bg-negative text-white q-mb-md">
            {{ errorMessage }}
            <template v-slot:action>
              <q-btn flat label="Dismiss" @click="errorMessage = ''" />
            </template>
          </q-banner>

          <q-linear-progress v-if="loading" color="primary" class="q-mb-md" />

          <q-list v-if="mints.length > 0" bordered separator>
            <q-expansion-item
              v-for="mint in mints"
              :key="mint.id"
              expand-separator
            >
              <template v-slot:header>
                <q-item-section>
                  <q-item-label>{{ mint.username }}</q-item-label>
                  <q-item-label caption>{{ mint.id }}</q-item-label>
                </q-item-section>
                <q-item-section side>
                  <div class="row q-gutter-xs">
                    <q-badge v-if="mint.sunset_mint" color="orange" label="Sunset" />
                    <q-badge
                      v-if="mint.verify_enabled"
                      color="blue"
                      label="Verify"
                    />
                  </div>
                </q-item-section>
                <q-item-section side>
                  <div class="row q-gutter-xs">
                    <q-btn
                      flat
                      dense
                      color="primary"
                      icon="edit"
                      @click.stop="openEditDialog(mint)"
                    ></q-btn>
                    <q-btn
                      flat
                      dense
                      color="negative"
                      icon="delete"
                      @click.stop="deleteMint(mint.id)"
                    ></q-btn>
                  </div>
                </q-item-section>
              </template>

              <q-card flat>
                <q-card-section>
                  <div class="row q-gutter-md">
                    <div class="col-6">
                      <div class="text-subtitle2 q-mb-sm">Outstanding Notes</div>
                      <q-linear-progress
                        v-if="notesLoading"
                        color="primary"
                        class="q-mb-sm"
                      />
                      <q-table
                        v-if="notes.length > 0"
                        dense
                        flat
                        :rows="notes"
                        :columns="noteColumns"
                        row-key="id"
                        :pagination="{ rowsPerPage: 5 }"
                      >
                        <template v-slot:body-cell-id="props">
                          <q-td :props="props">{{ truncateId(props.row.id) }}</q-td>
                        </template>
                        <template v-slot:body-cell-amount_msat="props">
                          <q-td :props="props">{{ formatSats(props.row.amount_msat) }}</q-td>
                        </template>
                        <template v-slot:body-cell-state="props">
                          <q-td :props="props">
                            <q-badge
                              :color="noteStateColor(props.row)"
                              :label="noteState(props.row)"
                            />
                          </q-td>
                        </template>
                      </q-table>
                      <div v-else-if="!notesLoading" class="text-grey text-caption">
                        No notes.
                      </div>
                      <q-btn
                        flat
                        dense
                        label="Refresh"
                        @click="fetchNotes(mint.id)"
                        class="q-mt-sm"
                      ></q-btn>
                    </div>
                    <div class="col-6">
                      <div class="text-subtitle2 q-mb-sm">Recent Activity</div>
                      <q-linear-progress
                        v-if="activityLoading"
                        color="primary"
                        class="q-mb-sm"
                      />
                      <q-table
                        v-if="activity.length > 0"
                        dense
                        flat
                        :rows="activity"
                        :columns="activityColumns"
                        row-key="payment_hash"
                        :pagination="{ rowsPerPage: 5 }"
                      >
                        <template v-slot:body-cell-type="props">
                          <q-td :props="props">
                            <q-badge
                              :color="props.row.type === 'mint' ? 'green' : 'purple'"
                              :label="props.row.type"
                            />
                          </q-td>
                        </template>
                        <template v-slot:body-cell-amount_msat="props">
                          <q-td :props="props">{{ formatSats(props.row.amount_msat) }}</q-td>
                        </template>
                        <template v-slot:body-cell-payment_hash="props">
                          <q-td :props="props">{{ truncateId(props.row.payment_hash) }}</q-td>
                        </template>
                        <template v-slot:body-cell-settled="props">
                          <q-td :props="props">
                            <q-badge
                              :color="props.row.settled ? 'green' : 'grey'"
                              :label="props.row.settled ? 'settled' : 'pending'"
                            />
                          </q-td>
                        </template>
                      </q-table>
                      <div v-else-if="!activityLoading" class="text-grey text-caption">
                        No activity yet.
                      </div>
                      <q-btn
                        flat
                        dense
                        label="Refresh"
                        @click="fetchActivity(mint.id)"
                        class="q-mt-sm"
                      ></q-btn>
                    </div>
                  </div>
                </q-card-section>
              </q-card>
            </q-expansion-item>
          </q-list>
          <div v-else-if="!loading" class="text-center text-grey q-pa-md">
            No mints yet. Click "Create Mint" to get started.
          </div>
        </q-card-section>
      </q-card>
    </div>

    <!-- Create Dialog -->
    <q-dialog v-model="createDialog.show">
      <q-card style="min-width: 400px">
        <q-card-section>
          <div class="text-h6">Create Mint</div>
        </q-card-section>
        <q-card-section>
          <q-form @submit.prevent="createMint">
            <q-input
              filled
              v-model="createDialog.data.username"
              label="Username"
              hint="A name for this mint"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="createDialog.data.base_fee_msat"
              label="Base Fee (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="createDialog.data.fee_percent_ppm"
              label="Fee Percent (ppm)"
              hint="Parts per million (10000 = 1%)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="createDialog.data.min_sendable_msat"
              label="Min Sendable (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="createDialog.data.max_sendable_msat"
              label="Max Sendable (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="createDialog.data.min_mint_msat"
              label="Min Mint (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              v-model="createDialog.data.base_url"
              label="Base URL"
              hint="Clearnet public URL (optional)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              v-model="createDialog.data.onion_url"
              label="Onion URL"
              hint="Tor onion address (optional)"
              class="q-mb-md"
            ></q-input>
            <div class="row items-center q-mb-md">
              <q-toggle
                v-model="createDialog.data.verify_enabled"
                label="Verify Enabled"
              />
              <q-toggle
                v-model="createDialog.data.sunset_mint"
                label="Sunset Mode"
                class="q-ml-md"
              />
            </div>
            <div class="row justify-end">
              <q-btn
                flat
                label="Cancel"
                v-close-popup
                class="q-mr-sm"
              ></q-btn>
              <q-btn
                unelevated
                color="primary"
                label="Create"
                type="submit"
                :loading="createDialog.loading"
              ></q-btn>
            </div>
          </q-form>
        </q-card-section>
      </q-card>
    </q-dialog>

    <!-- Edit Dialog -->
    <q-dialog v-model="editDialog.show">
      <q-card style="min-width: 400px">
        <q-card-section>
          <div class="text-h6">Edit Mint</div>
        </q-card-section>
        <q-card-section>
          <q-form @submit.prevent="updateMint">
            <q-input
              filled
              v-model="editDialog.data.username"
              label="Username"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="editDialog.data.base_fee_msat"
              label="Base Fee (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="editDialog.data.fee_percent_ppm"
              label="Fee Percent (ppm)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="editDialog.data.min_sendable_msat"
              label="Min Sendable (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="editDialog.data.max_sendable_msat"
              label="Max Sendable (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="editDialog.data.min_mint_msat"
              label="Min Mint (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              v-model="editDialog.data.base_url"
              label="Base URL"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              v-model="editDialog.data.onion_url"
              label="Onion URL"
              class="q-mb-md"
            ></q-input>
            <div class="row items-center q-mb-md">
              <q-toggle
                v-model="editDialog.data.verify_enabled"
                label="Verify Enabled"
              />
              <q-toggle
                v-model="editDialog.data.sunset_mint"
                label="Sunset Mode"
                class="q-ml-md"
              />
            </div>
            <div class="row justify-end">
              <q-btn
                flat
                label="Cancel"
                v-close-popup
                class="q-mr-sm"
              ></q-btn>
              <q-btn
                unelevated
                color="primary"
                label="Save"
                type="submit"
                :loading="editDialog.loading"
              ></q-btn>
            </div>
          </q-form>
        </q-card-section>
      </q-card>
    </q-dialog>
  </div>
`

window.PageLnurlmint = {
  template: LNURLMINT_TEMPLATE,
  data() {
    return {
      mints: [],
      loading: false,
      errorMessage: '',
      notes: [],
      notesLoading: false,
      activity: [],
      activityLoading: false,
      noteColumns: [
        { name: 'id', label: 'Note ID', field: 'id', align: 'left' },
        { name: 'amount_msat', label: 'Amount', field: 'amount_msat', align: 'right' },
        { name: 'state', label: 'State', field: 'state', align: 'center' }
      ],
      activityColumns: [
        { name: 'type', label: 'Type', field: 'type', align: 'center' },
        { name: 'amount_msat', label: 'Amount', field: 'amount_msat', align: 'right' },
        { name: 'payment_hash', label: 'Payment Hash', field: 'payment_hash', align: 'left' },
        { name: 'settled', label: 'Status', field: 'settled', align: 'center' }
      ],
      createDialog: {
        show: false,
        loading: false,
        data: {
          username: '',
          base_fee_msat: 0,
          fee_percent_ppm: 0,
          min_sendable_msat: 1000,
          max_sendable_msat: 1000000000,
          min_mint_msat: 10000,
          verify_enabled: true,
          sunset_mint: false,
          base_url: '',
          onion_url: ''
        }
      },
      editDialog: {
        show: false,
        loading: false,
        mintId: null,
        data: {
          username: '',
          base_fee_msat: 0,
          fee_percent_ppm: 0,
          min_sendable_msat: 1000,
          max_sendable_msat: 1000000000,
          min_mint_msat: 10000,
          verify_enabled: true,
          sunset_mint: false,
          base_url: '',
          onion_url: ''
        }
      }
    }
  },
  mounted() {
    this.fetchMints()
  },
  methods: {
    openCreateDialog() {
      this.createDialog.data = {
        username: '',
        base_fee_msat: 0,
        fee_percent_ppm: 0,
        min_sendable_msat: 1000,
        max_sendable_msat: 1000000000,
        min_mint_msat: 10000,
        verify_enabled: true,
        sunset_mint: false,
        base_url: '',
        onion_url: ''
      }
      this.createDialog.show = true
    },
    openEditDialog(mint) {
      this.editDialog.mintId = mint.id
      this.editDialog.data = {
        username: mint.username,
        base_fee_msat: mint.base_fee_msat,
        fee_percent_ppm: mint.fee_percent_ppm,
        min_sendable_msat: mint.min_sendable_msat,
        max_sendable_msat: mint.max_sendable_msat,
        min_mint_msat: mint.min_mint_msat,
        verify_enabled: mint.verify_enabled,
        sunset_mint: mint.sunset_mint,
        base_url: mint.base_url || '',
        onion_url: mint.onion_url || ''
      }
      this.editDialog.show = true
    },
    async fetchMints() {
      this.loading = true
      try {
        const wallet = this.g.user.wallets[0]
        const key = wallet.inkey || wallet.adminkey
        const response = await LNbits.api.request(
          'GET',
          '/lnurlmint/api/v1/mints',
          key
        )
        this.mints = Array.isArray(response.data) ? response.data : []
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.loading = false
      }
    },
    async createMint() {
      this.createDialog.loading = true
      try {
        const wallet = this.g.user.wallets[0]
        await LNbits.api.request(
          'POST',
          '/lnurlmint/api/v1/mints',
          wallet.adminkey,
          this.createDialog.data
        )
        this.createDialog.show = false
        this.fetchMints()
        this.$q.notify({message: 'Mint created', color: 'positive'})
      } catch (error) {
        LNbits.utils.notifyApiError(error)
        this.errorMessage = 'Failed to create mint'
      } finally {
        this.createDialog.loading = false
      }
    },
    async updateMint() {
      this.editDialog.loading = true
      try {
        const wallet = this.g.user.wallets[0]
        await LNbits.api.request(
          'PUT',
          '/lnurlmint/api/v1/mints/' + this.editDialog.mintId,
          wallet.adminkey,
          this.editDialog.data
        )
        this.editDialog.show = false
        this.fetchMints()
        this.$q.notify({message: 'Mint updated', color: 'positive'})
      } catch (error) {
        LNbits.utils.notifyApiError(error)
        this.errorMessage = 'Failed to update mint'
      } finally {
        this.editDialog.loading = false
      }
    },
    async deleteMint(mint_id) {
      try {
        const wallet = this.g.user.wallets[0]
        await LNbits.api.request(
          'DELETE',
          '/lnurlmint/api/v1/mints/' + mint_id,
          wallet.adminkey
        )
        this.fetchMints()
        this.$q.notify({message: 'Mint deleted', color: 'positive'})
      } catch (error) {
        if (error.response && error.response.status === 409) {
          this.errorMessage = 'Cannot delete mint with outstanding notes'
        } else {
          LNbits.utils.notifyApiError(error)
          this.errorMessage = 'Failed to delete mint'
        }
      }
    },
    async fetchNotes(mint_id) {
      this.notesLoading = true
      try {
        const wallet = this.g.user.wallets[0]
        const key = wallet.inkey || wallet.adminkey
        const response = await LNbits.api.request(
          'GET',
          '/lnurlmint/api/v1/mints/' + mint_id + '/notes',
          key
        )
        this.notes = response.data || []
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.notesLoading = false
      }
    },
    async fetchActivity(mint_id) {
      this.activityLoading = true
      try {
        const wallet = this.g.user.wallets[0]
        const key = wallet.inkey || wallet.adminkey
        const response = await LNbits.api.request(
          'GET',
          '/lnurlmint/api/v1/mints/' + mint_id + '/activity',
          key
        )
        this.activity = response.data || []
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.activityLoading = false
      }
    },
    formatSats(msat) {
      return (msat / 1000).toLocaleString() + ' sats'
    },
    truncateId(id) {
      return id ? id.slice(0, 12) + '...' : ''
    },
    noteState(note) {
      if (note.spent) return 'spent'
      if (note.pending) return 'pending'
      return 'outstanding'
    },
    noteStateColor(note) {
      if (note.spent) return 'grey'
      if (note.pending) return 'orange'
      return 'green'
    }
  }
}
